"""The digest: what the model may put into memory, and what the script takes back out.

Every test runs against a temporary home and vault, a temporary git identity and the fake
endpoint from M2.5, so nothing here reaches a model, ``~/afterthought``, ``~/.afterthought``
or the network (BUILD-PLAN 1.5). Transcripts are Claude Code's own JSONL, because that is
the file `aft.readers` is told to read.

What a digest wrote is read back off the page and out of the vault's git, and what it
refused is read back off the page too: a fact that exists only in the value a function
returned is not memory, and a watermark that ran ahead of its page is the failure this
module exists to make impossible (DESIGN 8, DESIGN 14).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from aft import (
    capture,
    cli,
    digest,
    evidence,
    inject,
    keys,
    lint,
    page,
    queue,
    registry,
    vault,
    work,
)
from aft import config as configmod
from aft import index as indexmod
from aft.config import Config
from aft.events import SESSION_END, SessionEvent
from aft.gitutil import GitError
from aft.model import ModelError
from aft.readers import CLAUDE, PI, Entry
from aft.readers.claude import SYNTHETIC_MODEL
from aft.redact import REDACTED
from conftest import make_repo
from fakes.model_server import FakeEndpoint

# The session's own clock: the entries are dated by it and the page is filed under it.
# A run that measures a quiet period or a day of tokens uses the host's clock instead, so
# the ledger it reads is the one its own calls wrote into.
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

SLUG = "widget"
SESSION = "s-1"

# The words the transcript below really holds. The quotes the facts cite are taken from
# them, which is what makes one fact honest and the next one invented.
USER_PROMPT = "Store the digest at session end, not per turn. Capture runs at session end."
ASSISTANT_ANSWER = "Done: the hook writes one row and returns without calling a model."
# The second exchange. DESIGN 8 skips a session that never got past one prompt, so a
# transcript standing for a session worth capturing holds this pair beside the first.
SECOND_PROMPT = "Give the retry its own setting, so a slow endpoint is not retried forever."
SECOND_ANSWER = "Done: the retry is read from config, and the endpoint's own hint wins there."
# A worker session's second exchange, for the same reason: DESIGN 8's floor asks two prompts
# of a worker session as of any other.
WORKER_AGAIN = "Worker note: the retry is what took the time."
WORKER_REPLY = "Noted: the retry is reported with the run."
# A prompt said after an off-the-record span has opened and never closed: work the rest of
# the session is not supposed to remember (DESIGN 12).
POST_SPAN = "The archive is worth its size in quotes."

PLAN = Path(__file__).resolve().parents[1] / "planning" / "BUILD-PLAN.md"


# ------------------------------------------------------------- what a test asks the world for


@dataclass(frozen=True)
class Session:
    """One registered project, a directory it was worked in, and one transcript of it."""

    cfg: Config
    cwd: Path
    transcript: Path


@pytest.fixture
def session(initialized: Config, tmp_path) -> Session:
    """A registered project, the repository a session of it was working in, its transcript."""
    cwd = make_repo(tmp_path / "dev" / SLUG).resolve()
    registered = registry.Registry.load(initialized.registry_path)
    project = registered.add(SLUG, "Widget")
    registered.add_path(project, registry.path_key(initialized.host, cwd))
    registered.save()
    return Session(initialized, cwd, write_transcript(tmp_path / "s-1.jsonl", cwd))


def write_records(path: Path, *records: dict) -> Path:
    """A transcript file holding exactly these records, in the order given."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for one in records:
            handle.write(json.dumps(one) + "\n")
    return path


def append_records(path: Path, *records: dict) -> Path:
    """A session that kept working: these records added to the end of the same file."""
    with path.open("a", encoding="utf-8") as handle:
        for one in records:
            handle.write(json.dumps(one) + "\n")
    return path


def write_transcript(path: Path, cwd: Path, *extra: dict) -> Path:
    """A Claude Code session file: two exchanges and a tool result between them."""
    return write_records(
        path,
        {"cwd": str(cwd)},
        record("e1", "user", USER_PROMPT),
        record("e2", "assistant", ASSISTANT_ANSWER, model="claude-real"),
        {
            "type": "user",
            "uuid": "e3",
            "timestamp": NOW.isoformat(),
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "written"}]
            },
        },
        record("e4", "user", SECOND_PROMPT),
        record("e5", "assistant", SECOND_ANSWER, model="claude-real"),
        *extra,
    )


def quiet_transcript(path: Path, cwd: Path) -> Path:
    """A session the filter keeps nothing of: a span that opens and is never closed."""
    return write_records(
        path,
        {"cwd": str(cwd)},
        record("e1", "user", "off the record: what the quarter's numbers really were"),
        record("e2", "assistant", "Understood. Nothing of it goes any further."),
    )


def record(
    entry_id: str,
    role: str,
    text: str,
    model: str | None = None,
    when: datetime = NOW,
) -> dict:
    """One conversation record of a Claude Code transcript, written at ``when``."""
    message: dict = {"content": [{"type": "text", "text": text}]}
    if model:
        message["model"] = model
    return {"type": role, "uuid": entry_id, "timestamp": when.isoformat(), "message": message}


@pytest.fixture(autouse=True)
def no_agent_sessions(tmp_path, monkeypatch):
    """No session directories: a sweep finds nothing unless the test writes some."""
    empty = tmp_path / "agent-sessions"
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: empty, PI: empty})


def serve(cfg: Config, *answers: object) -> tuple[FakeEndpoint, Config]:
    """Point the config at the fake endpoint, which answers with these objects in order.

    The whole file is rewritten because a run and a test both point at the endpoint, and
    a second ``[model]`` table in one config is a config that does not load.
    """
    fake = FakeEndpoint(list(answers))
    fake.start()
    (cfg.home / "config.toml").write_text(
        f'vault = "{cfg.vault}"\nhost = "{cfg.host}"\n'
        f'[model]\nendpoint = "{fake.endpoint}"\nname = "fake-model"\n',
        encoding="utf-8",
    )
    return fake, configmod.load()


def with_budget(cfg: Config, tokens: int) -> Config:
    """The same config.toml with the day's token budget set, re-read from disk.

    The budget is a config.toml setting like every other (DESIGN 6), so a test that needs
    a small one sets it the way a person does rather than reaching into ``aft.work``.
    """
    path = cfg.home / configmod.CONFIG_NAME
    text = path.read_text(encoding="utf-8")
    kept = text[: text.index("[work]")] if "[work]" in text else text
    path.write_text(f"{kept}[work]\ndaily_token_budget = {tokens}\n", encoding="utf-8")
    return configmod.load()


def said(
    statement: str = "Capture runs at session end, not per turn.",
    quote: str = "Capture runs at session end.",
    entry: str = "e1",
    kind: str = "decision",
    source: str = "stated",
    scope: str = "project",
    term: str = "long",
) -> dict:
    """One fact of the shape the schema describes, citing a real entry of the transcript."""
    return {
        "statement": statement,
        "kind": kind,
        "source": source,
        "term": term,
        "quote": quote,
        "entry": entry,
        "scope": scope,
    }


def answer(*facts: dict, summary: str = "Built the capture hook.", in_flight: str = "") -> dict:
    """One digest answer of the shape the schema describes."""
    return {
        "summary": summary,
        "handoff": {
            "in_flight": {"text": in_flight, "entry": "#1" if in_flight else ""},
            "failed": [],
            "open": [],
        },
        "facts": list(facts),
    }


def invented(statement: str = "Ted prefers rust for every hook.", quote: str = "rust") -> dict:
    """A fact whose quote is not the words of any entry that could carry it."""
    return said(statement=statement, quote=quote)


def event(transcript: Path, cwd: Path, session_id: str = SESSION) -> SessionEvent:
    """The capture event a session's end writes."""
    return SessionEvent(
        agent=CLAUDE,
        kind=SESSION_END,
        session_id=session_id,
        cwd=cwd,
        transcript_path=transcript,
        time=NOW,
        detail=None,
    )


def claim(cfg: Config, transcript: Path, cwd: Path, session_id: str = SESSION) -> queue.Job:
    """The job one capture event wrote, claimed as the digest worker claims it."""
    queue.enqueue(cfg, event(transcript, cwd, session_id))
    claimed = queue.next_job(cfg)
    assert claimed is not None
    return claimed


def digest_once(session: Session, *answers: object) -> digest.Digest:
    """Digest the session through the fake endpoint, which answers with these in order."""
    fake, cfg = serve(session.cfg, *answers)
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    return wrote


def frontmatter(cfg: Config, rel: str) -> dict:
    """The frontmatter of the page a digest just wrote."""
    written, _, _, error = page.split_frontmatter((cfg.vault / rel).read_text(encoding="utf-8"))
    assert error is None, error
    assert written is not None
    return written


def body(cfg: Config, rel: str) -> str:
    """The part of the page below its frontmatter."""
    _, rest, _, error = page.split_frontmatter((cfg.vault / rel).read_text(encoding="utf-8"))
    assert error is None, error
    return rest


def pages(cfg: Config) -> list[str]:
    """The digest pages in the vault, by path below ``digests``, sorted."""
    root = cfg.vault / "digests"
    if not root.is_dir():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.md") if p.is_file())


def rows(cfg: Config, status: str) -> list[str]:
    """The sessions with a job in this state, in the order the queue took them."""
    conn = sqlite3.connect(cfg.home / queue.QUEUE_NAME)
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT session_id FROM jobs WHERE status = ? ORDER BY created_time, id",
                (status,),
            )
        ]
    finally:
        conn.close()


def watermark(cfg: Config, session_id: str = SESSION) -> str | None:
    return queue.session_state(cfg, CLAUDE, session_id).watermark


def owed_digest(cfg: Config, session_id: str = SESSION) -> bool:
    """Whether the archive holds this session's words and no digest has read them yet."""
    return queue.session_state(cfg, CLAUDE, session_id).digest_due


def indexed_words(cfg: Config, needle: str) -> dict[str, int]:
    """How many rows of each search collection hold ``needle``, indexed or not.

    The count is read out of the index file rather than from what a function returned,
    because the question this answers is whether the words are searchable, and an empty
    result from an index that was never built would look like the same answer as an empty
    result from a session that was kept out (BUILD-PLAN M2.7).
    """
    counts: dict[str, int] = {}
    if not (cfg.home / indexmod.INDEX_NAME).exists():
        return counts
    conn = sqlite3.connect(cfg.home / indexmod.INDEX_NAME)
    try:
        for name in indexmod.COLLECTIONS:
            counts[name] = conn.execute(
                f"SELECT count(*) FROM {name}"
                " WHERE title LIKE ? OR description LIKE ? OR heading LIKE ? OR body LIKE ?",
                (f"%{needle}%",) * 4,
            ).fetchone()[0]
    finally:
        conn.close()
    return counts


def head(cfg: Config) -> str:
    """What the vault's own HEAD commit says, subject line and file list."""
    return subprocess.run(
        ["git", "log", "--name-only", "-1", "--format=%s"],
        cwd=cfg.vault,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def committed_files(cfg: Config) -> list[str]:
    """Every path the vault's history has taken, over every commit it holds."""
    return subprocess.run(
        ["git", "log", "--all", "--name-only", "--format="],
        cwd=cfg.vault,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


def page_path(session_id: str = SESSION) -> str:
    """Where a digest of ``NOW`` is filed, per DESIGN 6."""
    return f"digests/{below(session_id)}"


def below(session_id: str = SESSION, slug: str = SLUG) -> str:
    """The same path as `pages` reports it: below the ``digests`` directory itself."""
    return f"{slug}/{NOW:%Y}/{NOW:%m}/{NOW:%Y-%m-%d}-{CLAUDE}-{session_id}.md"


# ------------------------------------------------------- what may become memory, and what may not


def test_a_verified_fact_is_written_with_its_entry_and_its_quote(session: Session):
    """The keeping half of quote verification, so the dropping half cannot pass by silence.

    A verifier that refused everything would satisfy a test that only proved refusals, so
    this one reads the page's own fact line, the footnote on it, and the source that
    footnote names, holding the entry and the words the fact was taken from.
    """
    wrote = digest_once(session, answer(said()))
    assert wrote.path == page_path()
    line = f"- Capture runs at session end, not per turn. [stated] {NOW:%Y-%m-%d} [^s1]"
    assert line in body(session.cfg, wrote.path)
    source = frontmatter(session.cfg, wrote.path)["sources"][0]
    assert source["resource"] == f"aft://session/{CLAUDE}/{SESSION}#e1"
    assert source["quote"] == "Capture runs at session end."


def test_a_fabricated_quote_is_dropped_and_the_page_says_why(session: Session):
    """DESIGN 8: a fact whose quote is not in the entry it cites never reaches memory."""
    wrote = digest_once(session, answer(said(), invented()))
    assert [fact.statement for fact in wrote.facts] == [
        "Capture runs at session end, not per turn."
    ]
    refused = frontmatter(session.cfg, wrote.path)["aft"]["dropped"][0]
    assert refused["statement"] == "Ted prefers rust for every hook."
    assert refused["reason"] == "the quote is not found in entry e1"
    assert "rust" not in body(session.cfg, wrote.path)


def test_a_quote_taken_from_another_entry_is_dropped(session: Session):
    """The words have to be in the entry named, not merely somewhere in the session."""
    wrote = digest_once(session, answer(said(entry="e2", source="observed")))
    assert wrote.facts == ()
    refused = frontmatter(session.cfg, wrote.path)["aft"]["dropped"][0]
    assert refused["reason"] == "the quote is not found in entry e2"
    assert refused["entry"] == "e2"


def test_stated_on_an_assistants_own_entry_is_dropped(session: Session):
    """R2: what the agent declared is not a commitment by the user, however decisive it reads."""
    wrote = digest_once(
        session,
        answer(
            said(
                statement="The hook writes one row and returns.",
                quote="the hook writes one row and returns without calling a model",
                entry="e2",
                source="stated",
            )
        ),
    )
    assert wrote.facts == ()
    assert (
        frontmatter(session.cfg, wrote.path)["aft"]["dropped"][0]["reason"]
        == "[stated] cites entry e2, which is not one of the user's turns"
    )


def test_stated_on_a_users_own_entry_is_kept(session: Session):
    """The other half of R2: the user's own commitment stands, or the rule empties memory."""
    wrote = digest_once(session, answer(said()))
    assert [fact.statement for fact in wrote.facts] == [
        "Capture runs at session end, not per turn."
    ]
    assert "dropped" not in frontmatter(session.cfg, wrote.path)["aft"]


@pytest.mark.parametrize(
    ("broken", "reason"),
    [
        ({"kind": "habit"}, "unknown kind 'habit'"),
        ({"scope": "everywhere"}, "unknown scope 'everywhere'"),
    ],
)
def test_a_value_off_the_plans_list_never_reaches_memory(session: Session, broken, reason):
    """A fact off the plan's lists cannot become memory, by either of the two nets.

    The endpoint is asked in the plan's own schema, so an answer naming a kind or scope
    outside it is refused there and the job is left with that reason rather than a page.
    `digest.verify` is the second net for a caller that skipped the schema, and its own
    refusal is the one the reason text above belongs to.
    """
    from aft import filter as filtermod
    from aft.readers import read

    fake, cfg = serve(session.cfg, answer(said(**broken)))
    try:
        with pytest.raises(digest.AnswerRefused):
            digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert pages(cfg) == []
    assert watermark(cfg) is None

    # The failed call left its chunk archived, which is what a quote is checked against.
    entries, _ = filtermod.filter_entries(list(read(CLAUDE, session.transcript, None)), cfg)
    kept, dropped = digest.verify(cfg, SESSION, entries, [said(**broken)])
    assert kept == []
    assert [fact.reason for fact in dropped] == [reason]


def test_a_chunk_the_filter_empties_closes_the_job_with_no_page(session: Session):
    """An all-off-the-record chunk files nothing, because a page of nothing would end up
    standing for the whole day (DESIGN 8, DESIGN 12).

    The filter drops a span before anything is archived or asked, so a chunk that is
    nothing but one leaves the digest with no summary, no handoff and no fact. What it
    does leave is a decision: the mark moves past the entries read, so the next sweep and
    the next run hand the same chunk back as nothing, and the run counts the job among the
    sessions with nothing to digest.
    """
    quiet = quiet_transcript(session.transcript.with_name("quiet.jsonl"), session.cwd)
    fake, cfg = serve(session.cfg, answer(said()))
    queue.enqueue(cfg, event(quiet, session.cwd))
    try:
        first = work.run(cfg)
        asked = len(fake.requests)
        standing = pages(cfg)
        second = work.run(cfg)
    finally:
        fake.stop()
    assert first.stopped == ""
    assert first.digested == 0
    assert first.empty == 1, "an emptied chunk was not counted among the nothing-to-digest"
    assert asked == 0, "a chunk with nothing kept was asked of the model anyway"
    assert standing == []
    page_file = cfg.vault / page_path()
    assert not page_file.exists()
    assert list(page_file.parent.glob(".digest-*")) == []
    assert rows(cfg, queue.DONE) == [SESSION]
    assert watermark(cfg) == "e2"
    assert (second.swept, second.digested, second.empty) == (0, 0, 0)


def test_an_emptied_chunk_leaves_the_day_open_for_the_work_that_follows(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The checkpoint an emptied chunk moves must not swallow the session.

    Nothing here is off the record: the CLI's own bookkeeping is not conversation, a
    synthetic notice is infrastructure rather than an answer, and a tool result with
    nothing breaking in it is the filter's own documented drop. That chunk files no page,
    closes its row, and moves the mark past the entry it read. The work appended to the
    same transcript afterwards is then found by a later sweep and has the day's one path
    to itself, which is where the moved mark is either right or has eaten the session.
    """
    session_home = tmp_path / "claude-sessions"
    # The agents keep one directory per project, and the sweep walks that layout.
    (session_home / "widget").mkdir(parents=True)
    routine = write_records(
        session_home / "widget" / "s-1.jsonl",
        {"cwd": str(session.cwd)},
        {
            "type": "system",
            "uuid": "e0",
            "timestamp": NOW.isoformat(),
            "message": {"content": "workspace dirs initialized"},
        },
        {
            "type": "user",
            "uuid": "e1",
            "timestamp": NOW.isoformat(),
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
            },
        },
        {
            "type": "assistant",
            "uuid": "h1",
            "timestamp": NOW.isoformat(),
            "message": {
                "model": SYNTHETIC_MODEL,
                "content": [{"type": "text", "text": "nothing to add"}],
            },
        },
    )
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: session_home, PI: session_home})
    fake, cfg = serve(session.cfg, answer(said(entry="e2"), summary="Settled the hook's timing."))
    try:
        queue.enqueue(cfg, event(routine, session.cwd))
        first = work.run(cfg)
        asked = len(fake.requests)
        standing = pages(cfg)
        page_free = not (cfg.vault / page_path()).exists()
        mark = watermark(cfg, SESSION)
        rows_closed = rows(cfg, queue.DONE)
        with routine.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record("e2", "user", USER_PROMPT)) + "\n")
            handle.write(json.dumps(record("e3", "assistant", ASSISTANT_ANSWER)) + "\n")
            handle.write(json.dumps(record("e4", "user", SECOND_PROMPT)) + "\n")
            handle.write(json.dumps(record("e5", "assistant", SECOND_ANSWER)) + "\n")
        # The next scheduled run, after the session has gone quiet for its idle period.
        second = work.run(cfg, datetime.now(UTC) + capture.IDLE_AFTER)
        written = body(cfg, page_path())
    finally:
        fake.stop()
    assert first.empty == 1
    assert asked == 0, "a chunk with nothing kept was asked of the model anyway"
    assert standing == []
    assert page_free, "a page stood for the chunk that kept nothing"
    assert rows_closed == [SESSION], "the emptied chunk left its row in flight"
    assert mark == "e1#t1", "the mark did not move past the entry the chunk decided on"
    assert second.digested == 1, "the session was swallowed by the mark its emptied chunk moved"
    assert pages(cfg) == [below()], "the work that followed never reached a page"
    assert frontmatter(cfg, page_path())["description"] == "Settled the hook's timing."
    assert "Capture runs at session end, not per turn. [stated]" in written


def test_an_unclosed_span_runs_to_the_end_of_the_session(session: Session):
    """DESIGN 12: a span no prompt closes runs to the end of the session.

    A prompt opens the span and no prompt closes it, so the rest of the session is off the
    record: those entries reach no archive, no model, and no page, even when they arrive in
    a later chunk than the prompt that opened the span.
    """
    unclosed = write_records(
        session.transcript.with_name("unclosed.jsonl"),
        {"cwd": str(session.cwd)},
        record("e1", "user", USER_PROMPT),
        # A second prompt on the record, so the session is one the design captures and the
        # span, not the floor, is what keeps the rest of it out (DESIGN 8, DESIGN 12).
        record("p2", "user", SECOND_PROMPT),
        record("e2", "user", "off the record: the quarter's numbers"),
    )
    fake, cfg = serve(session.cfg, answer(said(entry="e1")), answer(said(entry="e3")))
    try:
        queue.enqueue(cfg, event(unclosed, session.cwd))
        work.run(cfg)
        with unclosed.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record("e3", "user", POST_SPAN)) + "\n")
            handle.write(json.dumps(record("e4", "assistant", ASSISTANT_ANSWER)) + "\n")
        queue.enqueue(cfg, event(unclosed, session.cwd))
        work.run(cfg)
        asked = json.dumps(fake.requests)
        written = body(cfg, page_path())
    finally:
        fake.stop()
    for words in ("the quarter's numbers", POST_SPAN):
        assert words not in asked, f"{words} reached the model after the span opened"
        assert words not in written, f"{words} reached the page after the span opened"
    assert not evidence.find_quote(cfg, SESSION, "e3", POST_SPAN)


def test_the_span_is_the_filters_to_decide(session: Session, monkeypatch: pytest.MonkeyPatch):
    """This module keeps no second copy of the spans state: it asks the filter and obeys.

    With the filter stood in for as one that keeps everything, the digest still hands the
    model every entry of the chunk, marker and all. What it drops, it drops because the
    filter said so - which is why the span can have exactly one owner. The span state the
    job starts with is the one the queue holds for the session, and the state the filter
    hands back is the one the job closes with."""
    unclosed = write_records(
        session.transcript.with_name("single-owner.jsonl"),
        {"cwd": str(session.cwd)},
        record("e1", "user", USER_PROMPT),
        record("e2", "user", "off the record: the quarter's numbers"),
        record("e3", "user", POST_SPAN),
    )
    asked_for: list[list[Entry]] = []
    started: list[bool] = []

    def keeps_everything(
        entries: Sequence[Entry], _config: Config, off_record: bool = False
    ) -> tuple[list[Entry], bool]:
        kept = list(entries)
        asked_for.append(kept)
        started.append(off_record)
        return kept, True

    monkeypatch.setattr(digest.filtermod, "filter_entries", keeps_everything)
    fake, cfg = serve(session.cfg, answer(said(entry="e1")))
    try:
        digest.digest_job(cfg, claim(cfg, unclosed, session.cwd))
        asked = json.dumps(fake.requests)
    finally:
        fake.stop()
    assert [entry.id for entry in asked_for[0]] == ["e1", "e2", "e3"]
    assert started == [False], "a first chunk began inside a span"
    assert queue.session_state(cfg, CLAUDE, SESSION).off_record, "the span state was not kept"
    for words in ("the quarter's numbers", POST_SPAN):
        assert words in asked, f"{words} went unasked though the filter kept it"


def test_an_answer_citing_an_entry_the_chunk_never_held_is_refused(session: Session):
    """An entry the model invents marks an answer about something other than the chunk, and
    its summary and handoff carry no quote to check, so the whole answer goes: asked once
    more, then the job fails with no page and the watermark where it was (finding T17)."""
    fake, cfg = serve(session.cfg, answer(said(entry="e99")), answer(said(entry="e99")))
    try:
        with pytest.raises(digest.AnswerRefused, match="cites entry 'e99'"):
            digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert len(fake.requests) == 2
    assert pages(cfg) == []
    assert watermark(cfg) is None


def test_an_answer_that_matches_on_the_second_asking_is_kept(session: Session):
    wrote = digest_once(session, answer(said(entry="e99")), answer(said()))
    assert [fact.statement for fact in wrote.facts] == [
        "Capture runs at session end, not per turn."
    ]


def with_handoff(key: str, text: str) -> dict:
    bad = answer(said())
    bad["handoff"][key] = [{"text": text, "entry": "#1"}]
    return bad


@pytest.mark.parametrize(
    "bad",
    [
        answer(said(statement="...")),
        answer(said(quote="…")),
        answer(said(statement=" ")),
        answer(said(), summary="..."),
        answer(said(), summary=""),
        answer(said(), in_flight="…"),
        with_handoff("failed", "--"),
        with_handoff("open", "..."),
    ],
)
def test_a_placeholder_answer_is_refused_and_its_entries_stay_unread(session: Session, bad: dict):
    fake, cfg = serve(session.cfg, bad, bad)
    try:
        with pytest.raises(digest.AnswerRefused, match="placeholder"):
            digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert len(fake.requests) == 2
    assert pages(cfg) == []
    assert watermark(cfg) is None


def test_the_chunk_is_counted_at_three_characters_and_the_answer_has_8000_tokens(
    session: Session,
):
    """F34's sizing: a transcript runs nearer three characters to a token than four, and an
    answer cut off at its cap is lost whole."""
    assert digest.tokens("x" * 3_000) == 1_000
    assert digest.MAX_CHUNK_TOKENS == 12_000
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert fake.requests[0]["max_tokens"] == 8_000


def test_the_model_sees_short_labels_and_the_page_records_entry_ids(session: Session):
    """A uuid copied wrongly loses its fact, so the model is shown #1, #2 and the script
    maps the label back to the entry's own id."""
    fake, cfg = serve(session.cfg, answer(said(entry="#1")))
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    sent = fake.requests[0]["messages"][1]["content"]
    assert sent.startswith("[#1] USER ")
    assert "[e1]" not in sent
    assert wrote is not None and [fact.entry for fact in wrote.facts] == ["e1"]
    assert f"aft://session/claude/{SESSION}#e1" in (session.cfg.vault / wrote.path).read_text()


def made_up() -> dict:
    """An answer about a session that never happened: every fact quotes words no entry
    holds (finding T17)."""
    return answer(
        invented("The grader scores look-alike pairs.", "5/8 on look-alike pairs"),
        invented("The dataset run was deferred.", "deferred to a future session"),
        summary="Validated the multiple-choice design against the full dataset.",
    )


def test_an_answer_whose_every_quote_is_missing_is_refused(session: Session):
    fake, cfg = serve(session.cfg, made_up(), made_up())
    try:
        with pytest.raises(digest.AnswerRefused, match="no fact's quote is in the entry"):
            digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert len(fake.requests) == 2
    assert pages(cfg) == []


def test_an_invented_answer_then_a_real_one_keeps_the_real_one(session: Session):
    wrote = digest_once(session, made_up(), answer(said(), summary="Built the capture hook."))
    written = frontmatter(session.cfg, wrote.path)
    assert written["description"] == "Built the capture hook."
    assert [fact.statement for fact in wrote.facts] == [
        "Capture runs at session end, not per turn."
    ]


def test_a_part_about_another_session_is_refused_and_the_rest_kept(session: Session):
    """F37 hands an invented part to F36: the part is recorded and refused, the others stay."""
    wide = long_transcript("wide-made-up.jsonl", session)
    fake, cfg = serve(session.cfg, made_up(), made_up(), answer(summary="The second part."))
    try:
        wrote = digest.digest_job(cfg, claim(cfg, wide, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    written = frontmatter(cfg, wrote.path)
    assert written["description"] == "The second part."
    [part] = written["aft"]["refused_parts"]
    assert "no fact's quote is in the entry it cites" in part["reason"]


def test_one_unquoted_fact_is_only_dropped(session: Session):
    """One missing quote is a fact the model got wrong, not an answer about another
    session: the fact is dropped and the answer stands."""
    wrote = digest_once(session, answer(invented()))
    assert wrote.facts == ()
    assert frontmatter(session.cfg, wrote.path)["description"] == "Built the capture hook."


def test_facts_dropped_for_another_reason_do_not_refuse_the_answer(session: Session):
    """Only quotes missing from their entries mark an invented answer; a [stated] fact on
    the agent's own turn is a misreading of who spoke, with real words behind it."""
    on_the_agents_turn = said(
        statement="The hook writes one row and returns.",
        quote="the hook writes one row and returns without calling a model",
        entry="e2",
        source="stated",
    )
    wrote = digest_once(session, answer(invented(), on_the_agents_turn))
    assert frontmatter(session.cfg, wrote.path)["description"] == "Built the capture hook."


def test_the_digest_prompt_says_an_empty_answer_is_a_good_one():
    assert "an answer with nothing in it is a good answer when nothing happened" in prompt_words()


def test_a_label_written_without_its_hash_is_read_as_the_label(session: Session):
    """A model writes "1" for "#1" as often as not; that names the entry, not a new one."""
    wrote = digest_once(session, answer(said(entry="1")))
    assert [fact.entry for fact in wrote.facts] == ["e1"]


def test_a_handoff_item_naming_no_entry_of_the_chunk_is_left_out(session: Session):
    """DESIGN 8: each handoff item names the entry it comes from; one naming none of the
    chunk's entries is not grounded in it."""
    grounded = answer(said())
    grounded["handoff"] = {
        "in_flight": {"text": "the sweep, still to write", "entry": "#9999"},
        "failed": [{"text": "a retry loop", "entry": "#1"}],
        "open": [{"text": "where the cursor lives", "entry": "nowhere"}],
    }
    wrote = digest_once(session, grounded)
    assert frontmatter(session.cfg, wrote.path)["aft"]["handoff"] == {
        "in_flight": "",
        "failed": ["a retry loop"],
        "open": [],
    }


def test_every_refusal_is_readable_on_the_page(session: Session):
    """`aft.dropped` is how "the verifier found nothing" reads differently from "broken"."""
    wrote = digest_once(
        session,
        answer(said(), invented(), said(entry="e2", quote="no words like these")),
    )
    dropped = frontmatter(session.cfg, wrote.path)["aft"]["dropped"]
    assert [fact["reason"] for fact in dropped] == [
        "the quote is not found in entry e1",
        "the quote is not found in entry e2",
    ]
    assert [fact["statement"] for fact in dropped] == [
        "Ted prefers rust for every hook.",
        "Capture runs at session end, not per turn.",
    ]
    assert [fact["entry"] for fact in dropped] == ["e1", "e2"]


def test_a_statement_the_page_writer_refuses_is_dropped_not_written(session: Session):
    """A statement that will not fit one line is a fact this digest did not write."""
    wrote = digest_once(session, answer(said(statement="one\nand another")))
    assert wrote.facts == ()
    reason = frontmatter(session.cfg, wrote.path)["aft"]["dropped"][0]["reason"]
    assert "one line" in reason


# ---------------------------------------------------------------- the page the digest leaves


def test_the_digest_page_lints(session: Session):
    """What `aft lint` reads of a page under ``digests``, proved on the page it wrote.

    The fact-line grammar and the footnote-to-source cross-check are gated on
    ``lint.FACT_DIRS`` - ``projects``, ``global``, ``references`` - so the rules that reach a
    digest page are the OKF frontmatter rules: its ``type``, its string fields, its
    ``generated`` pair, and the resource every source it carries. The page's own parse is
    what holds the written fact line, so both are read here - and a verdict of nothing is
    worth what the broken page below it proves, which is that the call does report this
    tree rather than walking past it.
    """
    wrote = digest_once(
        session,
        answer(
            said(),
            said(
                statement="The hook writes one row.",
                quote="the hook writes one row",
                entry="e2",
                kind="state",
                source="observed",
            ),
        ),
    )
    assert lint.lint_vault(session.cfg.vault) == []
    text = (session.cfg.vault / wrote.path).read_text(encoding="utf-8")
    assert page.parse(text, Path(wrote.path)).errors == []

    fm, rest, _, error = page.split_frontmatter(text)
    assert error is None and fm is not None
    del fm["sources"][0]["resource"]
    (session.cfg.vault / wrote.path).write_text(
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=100_000)
        + "---\n"
        + rest,
        encoding="utf-8",
    )
    assert [str(finding) for finding in lint.lint_vault(session.cfg.vault)] == [
        f"{wrote.path}:1: each source needs a resource"
    ]


def test_the_page_carries_the_projects_identity_and_the_handoff(session: Session):
    """DESIGN 6 and 10: the fields `aft inject` reads are the ones the digest writes."""
    wrote = digest_once(session, answer(said(), in_flight="the sweep, still to write"))
    written = frontmatter(session.cfg, wrote.path)
    registered = registry.Registry.load(session.cfg.registry_path).by_slug(SLUG)
    assert registered is not None
    assert written["type"] == "Session Digest"
    assert written["title"] == f"Widget session {NOW:%Y-%m-%d}"
    assert written["aft"]["project"] == registered.id
    assert written["aft"]["handoff"] == {
        "in_flight": "the sweep, still to write",
        "failed": [],
        "open": [],
    }


def test_the_summary_is_the_pages_description(session: Session):
    """DESIGN 6: a page's one-line summary is its ``description``, and it holds no prose."""
    wrote = digest_once(session, answer(said(), summary="Two facts and a handoff."))
    assert frontmatter(session.cfg, wrote.path)["description"] == "Two facts and a handoff."


def test_a_digest_page_is_its_owners_alone(session: Session):
    """A digest page is one sessions conversation, filed in a vault a person may share.

    Every other vault page comes from ``write_text`` at the umask's mode, so the digest's
    own file is the one that has to say its mode out loud. The mode is read back off the
    page a run wrote, not one the test set by hand.
    """
    wrote = digest_once(session, answer(said()))
    assert stat.S_IMODE((session.cfg.vault / wrote.path).stat().st_mode) == 0o600


def test_the_injection_reads_the_handoff_off_the_digest(session: Session):
    """The M1.2 contract: the digest this milestone writes is what a session starts with."""
    digest_once(session, answer(said(), in_flight="the sweep, still to write"))
    resolution = keys.resolve(
        session.cwd, registry.Registry.load(session.cfg.registry_path), session.cfg.host
    )
    block = inject.build(session.cfg, resolution)
    assert "## Last session" in block
    assert "- In flight: the sweep, still to write" in block


def test_a_global_fact_keeps_its_scope_and_its_place_on_the_page(session: Session):
    """A fact for every project is kept, and says so: the scope survives the verifier.

    One line of a fact page has no field for scope (DESIGN 6), so what the verifier verified
    travels in the record beside the page - the statement, its kind, its source, the entry
    its quote came from, and the scope that says it applies past this project (DESIGN 7). A
    fact the verifier refused is named there too, as dropped, and reaches no line at all.
    """
    wrote = digest_once(
        session,
        answer(
            said(
                statement="Ted prefers a plain hyphen.",
                quote="session end",
                kind="preference",
                scope="global",
            ),
            said(statement="The archive keeps its own words.", quote="rust", entry="e1"),
        ),
    )
    kept = frontmatter(session.cfg, wrote.path)["aft"]["facts"]
    assert kept == [
        {
            "statement": "Ted prefers a plain hyphen.",
            "kind": "preference",
            "source": "stated",
            "entry": "e1",
            "scope": "global",
            "term": "long",
        }
    ]
    assert "Ted prefers a plain hyphen. [stated]" in body(session.cfg, wrote.path)
    dropped = frontmatter(session.cfg, wrote.path)["aft"]["dropped"]
    assert [fact["statement"] for fact in dropped] == ["The archive keeps its own words."]


def test_a_global_fact_that_is_not_stated_carries_the_rule_it_will_be_skipped_for(
    session: Session,
):
    """DESIGN 9 R6: a global page changes only from [stated].

    The digest is not where that rule is applied, so an observed global fact is written like
    any other and carries the rule as its skip reason beside it: dreaming can report the skip
    without this page having to erase the fact first.
    """
    wrote = digest_once(
        session,
        answer(
            said(
                statement="The archive is worth its size in quotes.",
                quote="the hook writes one row",
                entry="e2",
                kind="state",
                source="observed",
                scope="global",
            ),
            said(
                statement="Ted prefers a plain hyphen.",
                quote="session end",
                kind="preference",
                scope="global",
            ),
        ),
    )
    kept = frontmatter(session.cfg, wrote.path)["aft"]["facts"]
    assert [fact["scope"] for fact in kept] == ["global", "global"]
    assert kept[0]["skip"] == "a global page changes only from [stated]"
    # The same sentence the digest module records, so a page and its code cannot drift apart.
    assert digest.GLOBAL_NOT_STATED == kept[0]["skip"]
    assert "skip" not in kept[1]
    assert "The archive is worth its size in quotes. [observed]" in body(session.cfg, wrote.path)
    assert [fact.scope for fact in wrote.facts] == ["global", "global"]


# ------------------------------------------------------- the order a crash leaves things in


def test_a_write_that_fails_leaves_no_page_and_no_watermark(session: Session):
    """The page's own path held by a directory: nothing lands, and nothing is marked read."""
    standing = session.cfg.vault / page_path()
    standing.parent.mkdir(parents=True, exist_ok=True)
    standing.mkdir()
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        with pytest.raises(OSError):
            digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert pages(cfg) == []
    assert list(standing.parent.glob(".digest-*")) == []  # no half-written temporary left
    assert watermark(cfg) is None


def test_a_commit_that_fails_leaves_no_page_standing(session: Session):
    """A page git never took is a page the watermark does not know: it goes back out.

    The repository stands and the vault's own pre-commit hook refuses the commit, so the
    page really is written and really is refused by git. A missing ``.git`` would be turned
    away by `vault.require` before a page was ever attempted, which is no test of a rollback
    at all - and a page left in git's index would be taken by the next commit of anything,
    so the history is read as well as the worktree.
    """
    hook = session.cfg.vault / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'this vault takes no digests' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        with pytest.raises(vault.VaultError) as raised:
            digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert "this vault takes no digests" in str(raised.value)
    page_file = cfg.vault / page_path()
    assert not page_file.exists()
    assert list(page_file.parent.glob(".digest-*")) == []
    assert watermark(cfg) is None
    assert rows(cfg, queue.DONE) == []

    hook.unlink()
    later = session.transcript.with_name("later.jsonl")
    shutil.copy(session.transcript, later)
    fake, cfg = serve(cfg, answer(said()))
    try:
        assert digest.digest_job(cfg, claim(cfg, later, session.cwd, session_id="s-2"))
    finally:
        fake.stop()
    assert pages(cfg) == [below("s-2")]
    assert page_path() not in committed_files(cfg)
    assert watermark(cfg) is None


def test_a_temporary_a_killed_run_leaves_is_no_page(session: Session, monkeypatch):
    """The window between writing a page and renaming it is one no rollback covers.

    The process is simply gone, so the temporary stands in the directory of the page it
    was becoming - a directory two other readers walk. Neither may treat it as a page:
    `aft lint` has nothing to report about it, and the injection still takes its handoff
    from the digest that really stands. The killed run is a month on, which is where its
    own page would have been filed and what makes the stray the newest file in the tree.
    """
    digest_once(session, answer(said(), in_flight="the sweep, still to write"))
    month_on = NOW.replace(month=11, day=2)
    later = write_records(
        session.transcript.with_name("later.jsonl"),
        {"cwd": str(session.cwd)},
        record("e1", "user", USER_PROMPT, when=month_on),
        record("e2", "assistant", ASSISTANT_ANSWER, model="claude-real", when=month_on),
        record("e3", "user", SECOND_PROMPT, when=month_on),
        record("e4", "assistant", SECOND_ANSWER, model="claude-real", when=month_on),
    )

    class Killed(BaseException):
        """What a signal after the creation and before the rename looks like."""

    def gone_before_it_is_written(*_args, **_kwargs):
        raise Killed

    fake, cfg = serve(session.cfg, answer(said(), in_flight="the chunk after this one"))
    job = claim(cfg, later, session.cwd, session_id="s-2")
    monkeypatch.setattr(digest.os, "fdopen", gone_before_it_is_written)
    try:
        with pytest.raises(Killed):
            digest.digest_job(cfg, job)
    finally:
        monkeypatch.undo()
        fake.stop()
    left = sorted(p.name for p in (cfg.vault / "digests" / SLUG / "2026" / "11").iterdir())
    assert len(left) == 1 and left[0].startswith(".digest-"), left
    assert pages(cfg) == [below()], "the killed run's page stood anyway"
    assert lint.lint_vault(cfg.vault) == []
    resolution = keys.resolve(session.cwd, registry.Registry.load(cfg.registry_path), cfg.host)
    assert "- In flight: the sweep, still to write" in inject.build(cfg, resolution)


def test_a_temporary_no_writer_can_still_claim_is_taken_away(session: Session):
    """A temporary is a page half-written, and age is all the vault can ask about it.

    The page is staged beside the page it will become, so a run killed on the way to the
    rename leaves that file in the month's directory for good unless the next writer clears
    it. The claim lease is how long a row may go untouched before another worker takes it
    back, so a temporary older than that has outlived every claim its maker could have
    held; a younger one is another process's page halfway to standing. Both are planted
    where this digest is about to file, and its own page lands besides neither of them.
    """
    directory = session.cfg.vault / "digests" / SLUG / f"{NOW:%Y}" / f"{NOW:%m}"
    directory.mkdir(parents=True)
    abandoned = directory / f"{digest.PAGE_TEMP_PREFIX}abandoned{digest.PAGE_TEMP_SUFFIX}"
    working = directory / f"{digest.PAGE_TEMP_PREFIX}working{digest.PAGE_TEMP_SUFFIX}"
    for stray in (abandoned, working):
        stray.write_text("half a page, from a run that did not finish", encoding="utf-8")
    outlived = (datetime.now(UTC) - queue.CLAIM_LEASE_FLOOR - timedelta(minutes=1)).timestamp()
    os.utime(abandoned, (outlived, outlived))
    digest_once(session, answer(said()))
    assert not abandoned.exists(), "a temporary no claim can cover still stood in the vault"
    assert working.is_file(), "another worker's page in progress was taken away"
    assert pages(session.cfg) == [below()], "the page beside the strays never arrived"


def test_the_watermark_moves_only_with_the_page_and_its_commit(session: Session):
    """Success looks like this: the page, its commit in the vault's own history, the mark."""
    wrote = digest_once(session, answer(said()))
    assert (session.cfg.vault / wrote.path).is_file()
    committed = head(session.cfg).splitlines()
    assert committed[0] == f"aft: digest {wrote.path}"
    assert committed[-1] == wrote.path
    # The last entry read, tool result included, not the last one kept: what the filter
    # threw away has been decided already and must not be decided again.
    assert wrote.watermark == watermark(session.cfg) == "e5"
    assert rows(session.cfg, queue.DONE) == [SESSION]


def test_a_rerun_on_the_same_session_adds_no_digest_page(session: Session):
    """Proved off the filesystem: the same session with nothing new leaves the tree alone."""
    wrote = digest_once(session, answer(said()))
    before = sorted((session.cfg.vault / "digests").rglob("*"))
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        again = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert again is None
    assert sorted((cfg.vault / "digests").rglob("*")) == before
    assert watermark(cfg) == wrote.watermark


def test_each_job_of_one_session_on_one_day_files_its_own_page(session: Session):
    """One page per job, and a page's bytes never change once it is written (DESIGN 8).

    A session that keeps working is digested again as it grows, so one day can hold several
    jobs for one session. Each takes the next part of the day's name - the plain name, then
    -2, then -3 - each spends its own one model answer, and every page filed before it keeps
    the bytes it was filed with, so the day's whole reading survives the last job.
    """
    grown = "The queue holds one row per session."
    ended = "The hook returns without a model call."
    stem = page_path()[:-3]
    fake, cfg = serve(
        session.cfg,
        answer(said()),
        answer(
            said(
                statement=grown,
                quote="one row per session",
                entry="e6",
                kind="state",
                source="observed",
            )
        ),
        answer(
            said(
                statement=ended,
                quote="without a model call",
                entry="e7",
                kind="state",
                source="observed",
            )
        ),
    )
    try:
        first = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
        filed_first = (cfg.vault / page_path()).read_bytes()
        append_records(session.transcript, record("e6", "user", grown))
        second = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
        filed_second = (cfg.vault / f"{stem}-2.md").read_bytes()
        append_records(session.transcript, record("e7", "user", ended))
        third = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
        asked = len(fake.requests)
        filed = pages(cfg)
    finally:
        fake.stop()
    assert first is not None and second is not None and third is not None
    assert [first.path, second.path, third.path] == [
        page_path(),
        f"{stem}-2.md",
        f"{stem}-3.md",
    ]
    assert asked == 3, "a job spent an answer another job should have had"
    assert filed == sorted([below(), f"{below()[:-3]}-2.md", f"{below()[:-3]}-3.md"])
    assert (cfg.vault / page_path()).read_bytes() == filed_first
    assert (cfg.vault / f"{stem}-2.md").read_bytes() == filed_second
    assert "Capture runs at session end, not per turn. [stated]" in body(cfg, first.path)
    assert f"{grown} [observed]" in body(cfg, second.path)
    assert f"{ended} [observed]" in body(cfg, third.path)
    assert watermark(cfg) == "e7"


def test_a_part_filed_while_the_model_answered_is_left_standing(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    """The part is chosen before the model call, and checked again under the vault lock.

    Another process working the same session (a job that outlived its claim lease while
    another run reclaimed it) files the part this job chose while this job waits on the
    model. The page this job writes takes the next free part, and the page that stood is
    left with the bytes it was filed with."""
    stem = page_path()[:-3]
    theirs = "---\ntype: Session Digest\ntitle: another job's page\n---\n"
    answered = digest._model_answer

    def answer_while_another_files(*args, **kwargs):
        standing = cfg.vault / page_path()
        standing.parent.mkdir(parents=True, exist_ok=True)
        standing.write_text(theirs, encoding="utf-8")
        with vault.locked(cfg):
            vault.commit(cfg.vault, [page_path()], "another job's digest")
        return answered(*args, **kwargs)

    monkeypatch.setattr(digest, "_model_answer", answer_while_another_files)
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    assert wrote.path == f"{stem}-2.md"
    assert (cfg.vault / page_path()).read_text(encoding="utf-8") == theirs
    assert "Capture runs at session end, not per turn. [stated]" in body(cfg, wrote.path)
    assert wrote.path in committed_files(cfg)
    assert watermark(cfg) == wrote.watermark


def test_a_directory_that_is_no_project_is_digested_unfiled(session: Session):
    """A session outside every project still reaches memory through its global facts.

    DESIGN 7: the session is digested and filed as unfiled, so what it says about the user
    and their machines is kept, while its project facts stay in the digest. No job fails.
    Under F49 a plain directory is a project, so the directory a session of no project runs
    in is under the system temporary directory, which resolves to none (DESIGN 7, F49).
    """
    nowhere = (Path(tempfile.gettempdir()) / "just-a-directory").resolve()
    nowhere.mkdir()
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, nowhere))
    finally:
        fake.stop()
    assert wrote is not None
    assert len(fake.requests) == 1
    assert pages(cfg) == [below(slug=digest.UNFILED)]
    written = frontmatter(cfg, wrote.path)
    assert "project" not in written["aft"]
    assert written["title"].startswith("Unfiled session ")
    assert watermark(cfg) is not None


def test_an_unregistered_repository_is_digested_under_its_own_name(session: Session):
    """The other side of the same line: a repository is a project the registry has not met.

    Its page carries no ``aft.project``, because a registry id it does not have would be
    a lie, but it is filed under the name of its own directory, where `aft key` says it
    lives (DESIGN 7), so the session is not lost for want of `aft project add`.
    """
    unregistered = make_repo(session.cwd.parent / "un-met").resolve()
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, unregistered))
    finally:
        fake.stop()
    assert wrote is not None
    assert wrote.path == f"digests/un-met/{NOW:%Y}/{NOW:%m}/{NOW:%Y-%m-%d}-{CLAUDE}-{SESSION}.md"
    written = frontmatter(cfg, wrote.path)
    assert "project" not in written["aft"]
    assert written["description"] == "Built the capture hook."


def test_a_live_directory_of_no_project_files_under_the_records_slug(session: Session):
    """The directory still stands and answers no project, and the name its session-start
    record settled while it was being keyed files the session (DESIGN 7, F49)."""
    plain = (Path(tempfile.gettempdir()) / "plain-widget").resolve()
    plain.mkdir(exist_ok=True)
    queue.record_project(session.cfg, CLAUDE, SESSION, None, "widget", plain)
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, plain))
    finally:
        fake.stop()
    assert wrote is not None
    assert wrote.path == f"digests/widget/{NOW:%Y}/{NOW:%m}/{NOW:%Y-%m-%d}-{CLAUDE}-{SESSION}.md"
    record = queue.recorded_project(cfg, CLAUDE, SESSION)
    assert record is not None and (record.slug, record.path) == ("widget", str(plain))


def test_the_home_directory_is_recorded_and_filed_like_any_project(session: Session):
    """The home directory is no exception to the keying: a project registered at it has its
    sessions recorded there and their digests filed under it (DESIGN 7, F49)."""
    registered = registry.Registry.load(session.cfg.registry_path)
    project = registered.add("homebase", "Homebase")
    registered.add_path(project, registry.path_key(session.cfg.host, session.cfg.home))
    registered.save()
    queue.record_project(session.cfg, CLAUDE, SESSION, project.id, "homebase", session.cfg.home)
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cfg.home))
    finally:
        fake.stop()
    assert wrote is not None
    assert wrote.path.startswith("digests/homebase/")
    written = frontmatter(cfg, wrote.path)
    assert written["aft"]["project"] == project.id


@pytest.mark.parametrize("escaped", ["../../escaped", "C:..outside"])
def test_a_session_id_that_is_not_a_name_is_refused_before_a_path_is_made(
    session: Session, escaped: str
):
    """A queue row's session id is another process's text, and a page is filed by it.

    The hook payload only had to be non-empty, so an id that walks out of ``digests/``
    reached a path, a directory, and a page before the commit refused the outcome. It is
    refused as that job's own failure, naming the id, with nothing filed and nothing asked
    of the model.
    """
    home_before = sorted(p.name for p in session.cfg.vault.parent.iterdir())
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        with pytest.raises(digest.DigestError) as raised:
            digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd, session_id=escaped))
    finally:
        fake.stop()
    assert escaped in str(raised.value)
    assert fake.requests == [], "an id that could not be filed was digested anyway"
    assert pages(cfg) == []
    assert sorted(p.name for p in cfg.vault.parent.iterdir()) == home_before
    assert watermark(cfg, escaped) is None


def test_a_transcript_that_vanished_closes_its_job_and_the_run_moves_on(session: Session):
    """T21: a session end whose file is gone has nothing more to read.

    The queue keeps the path the hook reported, and a run that reaches it later finds it
    deleted. Its job closes as having nothing to digest, with no model call and no failure,
    so it does not fail every later run; the other sessions are digested as usual.
    """
    vanished = session.transcript.with_name("gone.jsonl")
    shutil.copy(session.transcript, vanished)
    good = session.transcript.with_name("still-here.jsonl")
    shutil.copy(session.transcript, good)
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        queue.enqueue(cfg, event(vanished, session.cwd, session_id="gone-1"))
        vanished.unlink()
        queue.enqueue(cfg, event(good, session.cwd, session_id="here-1"))
        report = work.run(cfg)
    finally:
        fake.stop()
    assert report.digested == 1
    assert report.failures == ()
    assert len(fake.requests) == 1
    assert rows(cfg, queue.FAILED) == []
    assert sorted(rows(cfg, queue.DONE)) == ["gone-1", "here-1"]


def test_a_directory_deleted_since_the_session_is_digested_unfiled(session: Session, tmp_path):
    """A worktree cleaned up after its session is ordinary, and its words are still kept.

    Gate and orchestrator checkouts are made, worked in, and deleted by other processes, so by
    the time a run reaches such a session its directory cannot say which project it was. With
    nothing recorded at session start either, the session is archived under no project and
    digested as unfiled: no failure, and the run goes on (DESIGN 7, DESIGN 14).
    """
    gone = (tmp_path / "gate-worktree").resolve()
    gone.mkdir()
    here = session.transcript.with_name("here.jsonl")
    shutil.copy(session.transcript, here)
    fake, cfg = serve(session.cfg, answer(said()), answer(said()))
    try:
        queue.enqueue(cfg, event(session.transcript, gone, session_id="gone-cwd"))
        queue.enqueue(cfg, event(here, session.cwd, session_id="here-1"))
        shutil.rmtree(gone)
        report = work.run(cfg)
    finally:
        fake.stop()
    assert report.stopped == "", "a session-local path stopped the whole run"
    assert report.failures == ()
    assert report.digested == 2
    assert rows(cfg, queue.FAILED) == []
    assert sorted(rows(cfg, queue.DONE)) == ["gone-cwd", "here-1"]
    assert evidence.session_info(cfg, "gone-cwd") == (CLAUDE, None)
    assert sorted(pages(cfg)) == sorted([below("here-1"), below("gone-cwd", slug=digest.UNFILED)])


def test_a_deleted_directory_is_filed_under_the_project_its_session_started_in(
    session: Session, tmp_path
):
    """The project is decided at session start, while the directory stands (DESIGN 7).

    A worktree of a registered project is removed before the run reaches its session. The
    record the session-start hook left names the project, so the session is digested under it
    exactly as a session in the project's own checkout is.
    """
    project = registry.Registry.load(session.cfg.registry_path).by_slug(SLUG)
    gone = (tmp_path / "gate-worktree").resolve()
    gone.mkdir()
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        assert queue.record_project(cfg, CLAUDE, "gone-cwd", project.id, SLUG)
        queue.enqueue(cfg, event(session.transcript, gone, session_id="gone-cwd"))
        shutil.rmtree(gone)
        report = work.run(cfg)
    finally:
        fake.stop()
    assert report.failures == ()
    assert report.digested == 1
    assert pages(cfg) == [below("gone-cwd")]
    assert frontmatter(cfg, page_path("gone-cwd"))["aft"]["project"] == project.id
    assert evidence.session_info(cfg, "gone-cwd") == (CLAUDE, project.id)


def test_a_directory_moved_under_the_temporary_is_filed_under_the_record(
    session: Session, tmp_path
):
    """A directory that still stands but answers no project does not unfile its session.

    The directory was moved under the system temporary directory between session start and
    the digest, so the fresh resolve answers none. The record the session-start hook left
    while the directory stood names the project, and that is where the session is filed
    (DESIGN 7, F49) - deletion and move-aside file the session in the same place.
    """
    project = registry.Registry.load(session.cfg.registry_path).by_slug(SLUG)
    moved = (Path(tempfile.gettempdir()) / "moved-widget").resolve()
    moved.mkdir()
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        assert queue.record_project(cfg, CLAUDE, SESSION, project.id, SLUG, session.cwd)
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, moved))
    finally:
        fake.stop()
        shutil.rmtree(moved)
    assert wrote is not None
    assert pages(cfg) == [below(SESSION)]
    assert frontmatter(cfg, wrote.path)["aft"]["project"] == project.id
    assert evidence.session_info(cfg, SESSION) == (CLAUDE, project.id)


def test_a_deleted_directory_of_an_unregistered_project_keeps_its_recorded_name(
    session: Session, tmp_path
):
    """A project the registry does not hold yet is recorded by its slug alone, and a session
    whose directory is gone is digested under that name, as the directory would have been."""
    gone = (tmp_path / "scratch-tool").resolve()
    gone.mkdir()
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        assert queue.record_project(cfg, CLAUDE, "gone-cwd", None, "scratch-tool")
        queue.enqueue(cfg, event(session.transcript, gone, session_id="gone-cwd"))
        shutil.rmtree(gone)
        report = work.run(cfg)
    finally:
        fake.stop()
    assert report.failures == ()
    assert [rel for rel in pages(cfg) if rel.startswith("scratch-tool/")], pages(cfg)
    assert evidence.session_info(cfg, "gone-cwd") == (CLAUDE, None)


def test_an_unresolvable_directory_is_dropped_when_projects_are_excluded(
    session: Session, tmp_path
):
    """``capture.exclude_projects`` is answered by resolving the directory. One that is gone,
    with no project recorded, cannot be shown to be outside the exclusion, so it leaves no
    queue row, no evidence and no page."""
    gone = (tmp_path / "gate-worktree").resolve()
    gone.mkdir()
    fake, cfg = serve(session.cfg)
    try:
        cfg = with_capture(cfg, exclude_projects=["some-private-project"])
        queue.enqueue(cfg, event(session.transcript, gone, session_id="gone-cwd"))
        shutil.rmtree(gone)
        report = work.run(cfg)
        asked = len(fake.requests)
    finally:
        fake.stop()
    assert report.failures == ()
    assert asked == 0
    assert rows(cfg, queue.DONE) == [] and rows(cfg, queue.FAILED) == []
    assert evidence.entries(cfg, "gone-cwd") == []
    assert pages(cfg) == []


def test_forgetting_a_session_forgets_the_project_it_started_in(session: Session):
    """`aft forget session` leaves no trace in the queue, the recorded project included, and a
    forgotten session is not recorded again."""
    cfg = session.cfg
    assert queue.record_project(cfg, CLAUDE, SESSION, None, SLUG)
    assert queue.recorded_project(cfg, CLAUDE, SESSION) == queue.ProjectRecord(None, SLUG, None)
    queue.forget_session(cfg, SESSION)
    assert queue.recorded_project(cfg, CLAUDE, SESSION) is None
    assert not queue.record_project(cfg, CLAUDE, SESSION, None, SLUG)
    assert queue.recorded_project(cfg, CLAUDE, SESSION) is None


# ---------------------------------------------------------------- the chunk and its parts


def conversation(count: int, each: int) -> list[Entry]:
    """A run of entries of ``each`` characters each, as the split sees them."""
    return [
        Entry(
            f"e{index}",
            NOW + timedelta(minutes=index),
            "user" if index % 2 == 0 else "assistant",
            f"{index} " + ("filler " * (each // 7)),
            False,
        )
        for index in range(count)
    ]


def test_the_split_never_cuts_an_entry_and_never_loses_one():
    """Entries straddle the boundary and one is bigger than the whole budget.

    The parts are a partition: every entry arrives, in order, whole, in exactly one part.
    A split that dropped the entry which did not fit would keep every other property and
    lose a turn of the conversation, which is all a chunk is made of.
    """
    entries = conversation(6, 700)
    oversized = Entry(
        "e6", NOW + timedelta(minutes=9), "user", "x" * (digest.MAX_CHUNK_TOKENS * 5), False
    )
    entries.append(oversized)
    budget = digest.tokens(digest.render(entries[:2])) + 1  # room for two, never a third
    parts = digest.split_parts(entries, budget)
    assert [entry.id for part in parts for entry in part] == [entry.id for entry in entries]
    for part in parts:
        sent = digest.render(part)
        for entry in part:
            assert sent.count(entry.text) == 1  # whole, and once only
    assert [entry.id for entry in parts[-1]] == ["e6"]  # the oversized one stands alone


def long_transcript(name: str, session: Session, turns: int = 40) -> Path:
    """A session long enough that its chunk passes the chunk cap, in two parts.

    A kept entry is at most 1,500 characters (`aft.filter`), so the cap is reached by a
    long conversation rather than by one huge turn - which is also the only way the cap
    is ever reached in a real session, and the reason a part must be cut between entries.
    Each entry ends in a marker of its own so a part that cut one in half shows up.
    """
    path = session.transcript.with_name(name)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"cwd": str(session.cwd)}) + "\n")
        for index in range(1, turns + 1):
            kind = "user" if index % 2 else "assistant"
            model = None if kind == "user" else "claude-real"
            handle.write(json.dumps(record(f"e{index}", kind, turn(index), model=model)) + "\n")
    return path


def turn(index: int) -> str:
    """One turn of a long session: under the filter's own cap, and marked at both ends."""
    return f"entry {index} of the session: " + "filler word " * 100 + f" marker {index}"


def test_a_chunk_over_the_cap_is_sent_in_parts_of_whole_entries(session: Session):
    """The model gets more than one call, and never a conversation cut through an entry."""
    wide = long_transcript("wide.jsonl", session)
    fake, cfg = serve(session.cfg, answer(), answer())
    try:
        digest.digest_job(cfg, claim(cfg, wide, session.cwd))
    finally:
        fake.stop()
    sent = [request["messages"][1]["content"] for request in fake.requests]
    assert len(sent) == 2, "the chunk went to the model in one call"
    seen = [int(n) for call in sent for n in re.findall(r"entry (\d+) of the session", call)]
    assert seen == list(range(1, 41)), "an entry was lost, cut or repeated"
    for call in sent:
        assert digest.tokens(digest.prompt_text()) + digest.tokens(call) <= digest.MAX_CHUNK_TOKENS
    for call in sent:
        for index in range(1, 41):
            # An entry's words are all in the one call that holds it, or all in neither.
            assert call.count(turn(index)) == call.count(f"entry {index} of the session")


def test_two_parts_make_one_page_whole(session: Session):
    """One job, one page: both parts' summaries arrive uncut and the handoff is the last one.

    The schema caps a summary at 800 characters and a page's ``description`` at none, so
    two summaries at that cap are the boundary: an ellipsis or a quiet cut here would be
    the digest losing half a session behind a limit nobody asked it to enforce.
    """
    wide = long_transcript("wide-one-page.jsonl", session)
    first, second = "a" * 800, "b" * 800
    fake, cfg = serve(
        session.cfg,
        answer(summary=first),
        answer(summary=second, in_flight="open work"),
    )
    try:
        wrote = digest.digest_job(cfg, claim(cfg, wide, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    written = frontmatter(cfg, wrote.path)
    assert written["description"] == f"{first} {second}"
    assert written["aft"]["handoff"]["in_flight"] == "open work"
    assert pages(cfg) == [below()]


def refused() -> dict:
    """An answer about something other than the chunk: it cites an entry it never held."""
    return answer(said(entry="e99"))


def test_a_part_refused_twice_is_recorded_and_the_rest_of_the_job_is_kept(session: Session):
    """F36: one part the model cannot read does not lose a long session. The refused part
    gives the page nothing and is named with its entries; the watermark moves past it, and
    its words stay in the archive for a redigest."""
    wide = long_transcript("wide-refused.jsonl", session)
    fake, cfg = serve(session.cfg, refused(), refused(), answer(summary="The second part."))
    try:
        wrote = digest.digest_job(cfg, claim(cfg, wide, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    written = frontmatter(cfg, wrote.path)
    assert written["description"] == "The second part."
    [part] = written["aft"]["refused_parts"]
    assert part["first"] == "e1"
    assert "cites entry 'e99'" in part["reason"]
    assert watermark(cfg) == "e40"
    assert evidence.entries(cfg, SESSION)[0].id == "e1"


def test_a_refused_last_part_leaves_the_handoff_empty(session: Session):
    """The handoff says where the work stood at the end; a refused last part leaves nothing
    to say it, so an earlier part's handoff is not passed off as the end."""
    wide = long_transcript("wide-refused-last.jsonl", session)
    fake, cfg = serve(
        session.cfg, answer(summary="The first part.", in_flight="early work"), refused(), refused()
    )
    try:
        wrote = digest.digest_job(cfg, claim(cfg, wide, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    written = frontmatter(cfg, wrote.path)
    assert written["description"] == "The first part."
    assert written["aft"]["handoff"] == {"in_flight": "", "failed": [], "open": []}
    assert written["aft"]["refused_parts"][0]["last"] == "e40"


def test_redigest_keeps_the_parts_it_can_and_records_the_one_it_cannot(session: Session):
    wide = long_transcript("wide-redigest.jsonl", session)
    fake, cfg = serve(
        session.cfg,
        answer(summary="First."),
        answer(summary="Second."),
        refused(),
        refused(),
        answer(summary="Second, rebuilt."),
    )
    try:
        digest.digest_job(cfg, claim(cfg, wide, session.cwd))
        rebuilt = digest.redigest(cfg, SESSION, now=NOW)
    finally:
        fake.stop()
    written = frontmatter(cfg, rebuilt.path)
    assert written["description"] == "Second, rebuilt."
    assert written["aft"]["refused_parts"][0]["first"] == "e1"


def test_redacted_entries_respects_builtin_redaction(session: Session, tmp_path, monkeypatch):
    home = tmp_path / "home_nobuiltin_digest"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(
        f'vault = "{session.cfg.vault}"\nhost = "testhost"\n'
        "[capture]\n"
        "builtin_redaction = false\n"
        'redact_patterns = ["custom-secret-[0-9]+"]\n'
    )
    monkeypatch.setenv(configmod.HOME_ENV, str(home))
    nobuiltin_cfg = configmod.load()

    entries = [
        Entry(
            "e1",
            NOW,
            "user",
            "token ghp_notarealgithutpat0123456789 and custom-secret-12345",
            False,
        ),
    ]
    redacted = digest._redacted_entries(nobuiltin_cfg, entries)
    assert "ghp_notarealgithutpat0123456789" in redacted[0].text
    assert "custom-secret-12345" not in redacted[0].text
    assert REDACTED in redacted[0].text

    builtin_redacted = digest._redacted_entries(session.cfg, entries)
    assert "ghp_notarealgithutpat0123456789" not in builtin_redacted[0].text
    assert REDACTED in builtin_redacted[0].text


def test_a_job_whose_every_part_is_refused_fails(session: Session):
    wide = long_transcript("wide-all-refused.jsonl", session)
    fake, cfg = serve(session.cfg, refused(), refused(), refused(), refused())
    try:
        with pytest.raises(digest.AnswerRefused):
            digest.digest_job(cfg, claim(cfg, wide, session.cwd))
    finally:
        fake.stop()
    assert pages(cfg) == []
    assert watermark(cfg) is None


# ---------------------------------------------------------------- aft work


def test_work_sweeps_a_session_no_hook_reported_and_digests_it(session: Session, monkeypatch):
    """`aft work` is the sweep and the queue in one command, in that order (DESIGN 8)."""
    root = session.cfg.home / "claude-projects" / "-somewhere-widget"
    root.mkdir(parents=True)
    swept = root / "s-swept.jsonl"
    shutil.copy(session.transcript, swept)
    quiet = datetime.now(UTC) - timedelta(minutes=45)
    os.utime(swept, (quiet.timestamp(), quiet.timestamp()))
    fake, cfg = serve(session.cfg, answer(said()))
    monkeypatch.setattr(
        capture, "SESSION_DIRS", {CLAUDE: root.parent, PI: session.cfg.home / "no-pi-sessions"}
    )
    try:
        queue.watching_since(cfg, quiet - timedelta(hours=6))
        report = work.run(cfg)
    finally:
        fake.stop()
    assert report.swept == 1, "the session whose hook never fired was not swept"
    assert report.digested == 1
    assert len(pages(cfg)) == 1


def test_a_refused_answer_fails_its_own_job_and_the_run_carries_on(session: Session):
    """One session's unusable answer is that session's loss, not the queue's wedged.

    The plan's schema carries seven kinds and the model answers with an eighth, twice, so
    the answer is refused. That is this job's outcome alone: the row is marked failed with
    the model's own reason and its watermark unmoved, the next session is digested in the
    same run, and a run after that still digests.
    """
    refused = answer(said(kind="habit"))
    second = session.transcript.with_name("second-of-many.jsonl")
    shutil.copy(session.transcript, second)
    third = session.transcript.with_name("third-of-many.jsonl")
    shutil.copy(session.transcript, third)
    fake, cfg = serve(session.cfg, refused, refused, answer(said()), answer(said()))
    try:
        queue.enqueue(cfg, event(session.transcript, session.cwd, session_id="bad-1"))
        queue.enqueue(cfg, event(second, session.cwd, session_id="good-1"))
        first = work.run(cfg)
        reached = pages(cfg)
        queue.enqueue(cfg, event(third, session.cwd, session_id="later-1"))
        later = work.run(cfg)
    finally:
        fake.stop()
    assert first.digested == 1, "the refused answer stopped the whole run"
    assert first.stopped == ""
    assert len(first.failures) == 1
    assert "bad-1" in first.failures[0]
    assert "the answer failed its schema" in first.failures[0]
    assert "'habit'" in first.failures[0], "the model's own reason did not reach the report"
    assert rows(cfg, queue.FAILED) == ["bad-1"]
    assert watermark(cfg, "bad-1") is None
    assert reached == [below("good-1")], "the refused session's page was written anyway"
    assert later.digested == 1, "the queue was still wedged behind the refused answer"
    assert pages(cfg) == [below("good-1"), below("later-1")]


def test_the_daily_budget_stops_the_work_and_leaves_the_rest_queued(session: Session):
    """The jobs a run never reached are queued, unread, and waiting for the next one."""
    fake, cfg = serve(session.cfg, answer(said()), answer(said()), answer(said()))
    for index in range(3):
        path = session.transcript.with_name(f"session-{index}.jsonl")
        shutil.copy(session.transcript, path)
        queue.enqueue(cfg, event(path, session.cwd, session_id=f"s-{index}"))
    cfg = with_budget(cfg, 250)  # one call costs 200 + 50
    try:
        first = work.run(cfg)
        reached = pages(cfg)
        waiting = rows(cfg, queue.PENDING)
        marks = {f"s-{index}": watermark(cfg, f"s-{index}") for index in range(3)}
        second = work.run(with_budget(cfg, 100_000))  # the cap lifted, the same day
    finally:
        fake.stop()
    assert first.stopped == work.STOP_BUDGET
    assert first.digested == 1
    assert len(reached) == 1
    assert sorted(waiting) == ["s-1", "s-2"]
    assert marks == {"s-0": "e5", "s-1": None, "s-2": None}
    assert second.stopped == ""
    assert second.digested == 2
    assert len(pages(cfg)) == 3


def test_a_claim_another_worker_took_over_reaches_the_report_not_a_traceback(
    session: Session, monkeypatch
):
    """A row reclaimed mid-digest is a reported stop, and its page is not undone.

    A chunk that outran its claim lease while a second `aft work` was sweeping is the
    ordinary way a claim is lost. The queue refuses to close a row another process holds,
    and that refusal belongs on the report a person and a timer read (DESIGN 14) - along
    with the page the digest had already committed, which the failed close does not erase.
    """
    real = digest.digest_job

    def digest_while_it_is_taken(config, job, moment=None):
        conn = sqlite3.connect(config.home / queue.QUEUE_NAME)
        conn.execute(
            "UPDATE jobs SET status = ?, claimed_pid = NULL WHERE id = ?",
            (queue.PENDING, job.id),
        )
        conn.commit()
        conn.close()
        return real(config, job, moment)

    fake, cfg = serve(session.cfg, answer(said()))
    queue.enqueue(cfg, event(session.transcript, session.cwd, session_id="taken-1"))
    monkeypatch.setattr(digest, "digest_job", digest_while_it_is_taken)
    try:
        report = work.run(cfg)
    finally:
        fake.stop()
    assert report.stopped == work.STOP_QUEUE
    assert len(report.failures) == 1
    assert "not held by this process" in report.failures[0]
    assert below("taken-1") in pages(cfg), "the committed digest was lost with the close"
    assert watermark(cfg, "taken-1") is None


def test_a_claim_lost_while_failing_a_job_reaches_the_command_too(
    session: Session, monkeypatch, capsys
):
    """The queue call that writes a failure down can lose its claim in the same way.

    It sits in the handler that reports the job's own failure, so the run would die in the
    middle of reporting if the queue's refusal were not reported too: the job's reason and
    the queue's both arrive on the command's lines, the run stops for the queue, the row is
    left for whoever holds it now, and a timer sees a status (DESIGN 14).
    """
    gone = session.transcript.with_name("gone.jsonl")
    shutil.copy(session.transcript, gone)

    def digest_after_it_is_taken(config, job, moment=None):
        conn = sqlite3.connect(config.home / queue.QUEUE_NAME)
        conn.execute(
            "UPDATE jobs SET status = ?, claimed_pid = NULL WHERE id = ?",
            (queue.PENDING, job.id),
        )
        conn.commit()
        conn.close()
        raise digest.DigestError(f"cannot read {gone}")

    fake, cfg = serve(session.cfg, answer(said()))
    queue.enqueue(cfg, event(gone, session.cwd, session_id="lost-1"))
    monkeypatch.setattr(digest, "digest_job", digest_after_it_is_taken)
    try:
        code = cli.main(["work"])
        printed = capsys.readouterr()
    finally:
        fake.stop()
    assert code == 1
    assert "stopped: the queue could not be used" in printed.out
    assert any("gone.jsonl" in line for line in printed.err.splitlines())
    assert any("not held by this process" in line for line in printed.err.splitlines())
    assert rows(cfg, queue.FAILED) == []
    assert rows(cfg, queue.PENDING) == ["lost-1"]


def test_a_queue_that_cannot_be_opened_stops_the_run_with_its_own_words(session: Session):
    """A locked or unusable queue database is a reported stop, not a traceback (DESIGN 14)."""
    (session.cfg.home / queue.QUEUE_NAME).mkdir()
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        report = work.run(cfg)
    finally:
        fake.stop()
    assert report.stopped == work.STOP_QUEUE
    assert report.digested == 0
    assert fake.requests == [], "a run with no queue asked the model anyway"
    assert pages(cfg) == []
    assert "unable to open database file" in report.failures[0]


def test_one_unreadable_session_does_not_stop_the_others(session: Session):
    """A transcript this reader cannot read is that session's failure, and the run moves on."""
    broken = session.transcript.with_name("broken.jsonl")
    broken.write_text('{"cwd": "nowhere"}\nnot json at all\n', encoding="utf-8")
    good = session.transcript.with_name("good.jsonl")
    shutil.copy(session.transcript, good)
    fake, cfg = serve(session.cfg, answer(said()), answer(said()))
    try:
        queue.enqueue(cfg, event(broken, session.cwd, session_id="broken-1"))
        queue.enqueue(cfg, event(good, session.cwd, session_id="good-1"))
        report = work.run(cfg)
    finally:
        fake.stop()
    assert report.digested == 1
    assert len(report.failures) == 1
    assert rows(cfg, queue.FAILED) == ["broken-1"]
    assert rows(cfg, queue.DONE) == ["good-1"]
    assert report.stopped == ""


def test_an_endpoint_that_is_down_stops_the_run_and_keeps_the_work_queued(session: Session):
    """DESIGN 14: a dead endpoint is a stopped run, not a queue stamped job by job."""
    (session.cfg.home / "config.toml").write_text(
        (session.cfg.home / "config.toml").read_text(encoding="utf-8")
        + '[model]\nendpoint = "http://127.0.0.1:9/v1"\nname = "fake-model"\n',
        encoding="utf-8",
    )
    cfg = configmod.load()
    path = session.transcript.with_name("session-down.jsonl")
    shutil.copy(session.transcript, path)
    queue.enqueue(cfg, event(path, session.cwd, session_id="s-down"))
    report = work.run(cfg)
    assert report.stopped == work.STOP_UNREACHABLE
    assert report.digested == 0
    assert pages(cfg) == []
    assert watermark(cfg, "s-down") is None
    # The row is the one this run touched: nothing left in the queue was read or refused.
    assert rows(cfg, queue.FAILED) == []


def test_an_index_written_by_another_build_stops_the_run_naming_the_index(
    session: Session, monkeypatch
):
    """DESIGN 14, lead's ruling on #39: an index that refuses is not an endpoint that is down.

    The digest step indexes before it spends a token, so the index's refusal arrives as one of
    the failures that end a run. Told as the endpoint's word it sends whoever reads the timer's
    line to a model that is fine, when the repair is `aft reindex` and no model at all.
    """
    fake, cfg = serve(session.cfg, answer(said()))
    queue.enqueue(cfg, event(session.transcript, session.cwd, session_id="stale-1"))
    assert indexmod.update(cfg) == []
    monkeypatch.setattr(indexmod, "BUILD_STAMP", "the-next-build")
    try:
        report = work.run(cfg)
    finally:
        fake.stop()
    assert report.stopped == work.STOP_INDEX
    assert any("written by build rows" in line for line in report.failures), report.failures
    assert report.digested == 0
    assert pages(cfg) == [], "the refusal came after the page stood"
    assert watermark(cfg, "stale-1") is None
    assert rows(cfg, queue.FAILED) == []


def test_the_command_runs_the_work_and_says_what_it_did(session: Session, capsys):
    """`aft work` is a command a person and a timer can run, not only a library call."""
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        queue.enqueue(cfg, event(session.transcript, session.cwd))
        code = cli.main(["work"])
    finally:
        fake.stop()
    assert code == 0
    assert "1 digested" in capsys.readouterr().out


def test_the_work_reports_a_failure_as_a_nonzero_exit(session: Session, capsys):
    """A timer needs a status to page on, not a line only its own log holds."""
    broken = session.transcript.with_name("broken.jsonl")
    broken.write_text('{"cwd": "nowhere"}\nnot json at all\n', encoding="utf-8")
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        queue.enqueue(cfg, event(broken, session.cwd))
        code = cli.main(["work"])
    finally:
        fake.stop()
    assert code == 1
    assert "1 failed" in capsys.readouterr().out


# --------------------------------------------------------- the prompt and the schema shipped


def test_the_prompt_shipped_is_the_ones_text_in_the_plan():
    """The plan's own words are the prompt; a quietly rewritten prompt is a new system."""
    lines = PLAN.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if line.startswith("- **Prompt**"))
    stop = next(index for index, line in enumerate(lines) if line.startswith("- **Schema**"))
    fences = [
        index
        for index, line in enumerate(lines[start:stop], start=start)
        if line.strip() in ("```text", "```")
    ]
    assert len(fences) == 2, "the plan's prompt block is not the fenced pair this reads"
    assert digest.prompt_text() == "".join(lines[fences[0] + 1 : fences[1]])


def test_the_schema_holds_the_keys_limits_and_enums_the_plan_gives():
    """Every bound in the plan's own sentence, in the file the model is asked with."""
    schema = digest.digest_schema()
    assert schema["required"] == ["summary", "handoff", "facts"]
    assert schema["properties"]["summary"]["maxLength"] == 800
    facts = schema["properties"]["facts"]
    assert facts["maxItems"] == 20
    fact = facts["items"]
    assert fact["required"] == ["statement", "kind", "source", "term", "quote", "entry", "scope"]
    assert fact["properties"]["term"]["enum"] == ["long", "short"]
    assert fact["properties"]["statement"]["maxLength"] == 300
    assert fact["properties"]["quote"]["maxLength"] == 300
    assert fact["properties"]["kind"]["enum"] == [
        "preference",
        "decision",
        "state",
        "fact",
        "lesson",
        "procedure",
        "reference",
    ]
    assert fact["properties"]["source"]["enum"] == [
        "stated",
        "observed",
        "inferred",
        "suggested",
    ]
    assert fact["properties"]["scope"]["enum"] == ["project", "global"]
    handoff = schema["properties"]["handoff"]
    assert handoff["required"] == ["in_flight", "failed", "open"]
    assert handoff["properties"]["in_flight"]["properties"]["text"]["maxLength"] == 400
    assert handoff["properties"]["in_flight"]["required"] == ["text", "entry"]
    assert handoff["properties"]["open"]["items"]["required"] == ["text", "entry"]
    assert handoff["properties"]["failed"]["maxItems"] == 5
    assert handoff["properties"]["open"]["maxItems"] == 5


def test_the_schema_is_one_the_validator_reads():
    """`aft.schema` is what constrains the answer, so its own checks run before any call."""
    from aft import schema

    schema.check(digest.digest_schema())
    with pytest.raises(schema.SchemaError):
        schema.validate(
            {
                "summary": "x" * 801,
                "handoff": {"in_flight": {"text": "", "entry": ""}, "failed": [], "open": []},
                "facts": [],
            },
            digest.digest_schema(),
        )


def with_capture(
    cfg: Config,
    *,
    worker_paths: Sequence[str] = (),
    exclude_paths: Sequence[str] = (),
    exclude_projects: Sequence[str] = (),
    budget: int | None = None,
) -> Config:
    """Rewrite config.toml with capture paths and optional token budget."""
    path = cfg.home / configmod.CONFIG_NAME
    text = path.read_text(encoding="utf-8")
    wp = json.dumps(list(worker_paths))
    ep = json.dumps(list(exclude_paths))
    xp = json.dumps(list(exclude_projects))
    text = re.sub(r"\n\[capture\][\s\S]*?(?=\n\[|\Z)", "", text)
    text = re.sub(r"\n\[work\][\s\S]*?(?=\n\[|\Z)", "", text)
    text += f"\n[capture]\nworker_paths = {wp}\nexclude_paths = {ep}\n"
    if exclude_projects:
        text += f"exclude_projects = {xp}\n"
    if budget is not None:
        text += f"\n[work]\ndaily_token_budget = {budget}\n"
    path.write_text(text, encoding="utf-8")
    return configmod.load()


# The words of a session whose directory sits under capture.exclude_paths. Finding them in
# the queue, the archive, the index or the vault is the failure this task exists to prevent.
SECRET_WORDS = "super secret prompt"


def test_excluded_session_leaves_no_trace(session: Session, tmp_path):
    """An excluded session leaves no queue row, no evidence, no index row, and no digest.

    Each of the four is checked where it would have been written: the queue by the sweep and
    again by an enqueue of the session's event, the evidence and the index by a whole
    `aft work`, the digest by the page directory. The index is built before it is read,
    because an index file that was never written answers "not there" to every question.
    """
    excluded_dir = tmp_path / "private_repo"
    excluded_dir.mkdir()
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        cfg = with_capture(cfg, exclude_paths=[str(excluded_dir)])

        transcript = write_records(
            tmp_path / "excluded.jsonl",
            {"cwd": str(excluded_dir)},
            record("e1", "user", SECRET_WORDS),
        )

        swept = capture.sweep(cfg, NOW)
        assert not any(ev.cwd == excluded_dir for ev in swept)
        assert rows(cfg, queue.PENDING) == []

        ev = event(transcript, excluded_dir, session_id="excluded-1")
        queue.enqueue(cfg, ev)
        assert rows(cfg, queue.PENDING) == []

        conn = sqlite3.connect(cfg.home / queue.QUEUE_NAME)
        conn.execute(
            "INSERT INTO jobs (id, agent, session_id, cwd, transcript_path, kind,"
            " event_time, status, created_time, updated_time)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "job_ex",
                CLAUDE,
                "excluded-1",
                str(excluded_dir),
                str(transcript),
                SESSION_END,
                NOW.isoformat(),
                queue.PENDING,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        report = work.run(cfg)
        assert report.digested == 0

        conn = sqlite3.connect(cfg.home / queue.QUEUE_NAME)
        all_jobs = conn.execute("SELECT id FROM jobs WHERE session_id = 'excluded-1'").fetchall()
        conn.close()
        assert all_jobs == []

        assert evidence.entries(cfg, "excluded-1") == []

        indexmod.update(cfg)
        assert indexed_words(cfg, SECRET_WORDS) == dict.fromkeys(indexmod.COLLECTIONS, 0)

        assert not list((cfg.vault / "digests").rglob("*.md"))
    finally:
        fake.stop()


def test_an_excluded_job_is_dropped_before_its_transcript_is_read(session: Session, tmp_path):
    """The digest step refuses an excluded job before opening its file.

    The job names a transcript that does not exist: a guard placed after the read would
    fail this test with the reader's own error rather than the quiet None the guard
    returns, so the assertion is the proof that nothing was opened (DESIGN 12).
    """
    excluded_dir = tmp_path / "private_repo"
    excluded_dir.mkdir()
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        cfg = with_capture(cfg, exclude_paths=[str(excluded_dir)])
        missing = tmp_path / "never-opened.jsonl"

        job = queue.Job(
            id="job_ex",
            agent=CLAUDE,
            session_id="excluded-1",
            cwd=excluded_dir,
            transcript_path=missing,
            kind=SESSION_END,
            event_time=NOW,
            watermark=None,
            off_record=False,
        )
        assert digest.digest_job(cfg, job) is None

        assert evidence.entries(cfg, "excluded-1") == []
        assert len(fake.requests) == 0, "an excluded session reached the model"
    finally:
        fake.stop()


def test_a_digested_sessions_words_do_reach_the_index(session: Session):
    """The other half of the exclusion claim: a session that was read is searchable.

    Without this the empty index counts in the exclusion tests prove nothing, since the
    same zeros appear when the index holds nothing at all.
    """
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        queue.enqueue(cfg, event(session.transcript, session.cwd))
        report = work.run(cfg)
        assert report.digested == 1

        found = indexed_words(cfg, USER_PROMPT[:32])
        assert found[indexmod.EVIDENCE] > 0, "the session's own words are not searchable"
    finally:
        fake.stop()


def test_worker_session_is_archived_and_indexed_but_not_digested_when_budget_is_short(
    session: Session, tmp_path
):
    """A worker session is archived and indexed, but digested only when budget has room."""
    worker_dir = make_repo(tmp_path / "worker_repo").resolve()
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        cfg = with_capture(cfg, worker_paths=[str(worker_dir)], budget=0)

        transcript = write_records(
            tmp_path / "worker.jsonl",
            {"cwd": str(worker_dir)},
            record("e1", "user", "Worker prompt: the test suite failed with exit 1."),
            record(
                "e2", "assistant", "Understood, investigating the failure.", model="claude-real"
            ),
            record("e3", "user", WORKER_AGAIN),
            record("e4", "assistant", WORKER_REPLY, model="claude-real"),
        )
        queue.enqueue(cfg, event(transcript, worker_dir, session_id="worker-s1"))
        assert rows(cfg, queue.PENDING) == ["worker-s1"]

        report = work.run(cfg)

        assert report.stopped == work.STOP_BUDGET
        assert report.digested == 0
        assert len(fake.requests) == 0

        archived = evidence.entries(cfg, "worker-s1")
        assert len(archived) >= 1
        assert any("Worker prompt" in e.text for e in archived)

        conn = sqlite3.connect(cfg.home / "index.sqlite")
        indexed_count = conn.execute(
            "SELECT count(*) FROM evidence WHERE session_id = 'worker-s1'"
        ).fetchone()[0]
        conn.close()
        assert indexed_count > 0

        assert pages(cfg) == []
        # The row is finished work, not work waiting: a waiting row for an archived worker is
        # what stopped capture following the session at all. What the run still owes is the
        # digest, and that is the debt on the session row - with the watermark left where it
        # was, so the digest reads this same chunk when the budget has room (M2.7).
        assert rows(cfg, queue.PENDING) == []
        assert rows(cfg, queue.DONE) == ["worker-s1"]
        assert owed_digest(cfg, "worker-s1") is True
        assert watermark(cfg, "worker-s1") is None
    finally:
        fake.stop()


def test_worker_session_digested_with_budget_left_after_primary(session: Session, tmp_path):
    """Primary sessions are digested first; worker sessions are digested with leftover budget,
    carrying aft.role: worker in frontmatter and facts.
    """
    worker_dir = make_repo(tmp_path / "worker_repo").resolve()
    fake, cfg = serve(
        session.cfg,
        answer(
            said(
                statement="Primary decision made.",
                quote="Store the digest at session end",
                entry="e1",
            )
        ),
        answer(said(statement="Worker test passed.", quote="Worker ran tests", entry="w1")),
    )
    try:
        cfg = with_capture(cfg, worker_paths=[str(worker_dir)], budget=500_000)

        primary_transcript = session.transcript
        queue.enqueue(cfg, event(primary_transcript, session.cwd, session_id="primary-1"))

        worker_transcript = write_records(
            tmp_path / "worker.jsonl",
            {"cwd": str(worker_dir)},
            record("w1", "user", "Worker ran tests: all green."),
            record("w2", "assistant", "Recorded test results.", model="claude-real"),
            record("w3", "user", WORKER_AGAIN),
            record("w4", "assistant", WORKER_REPLY, model="claude-real"),
        )
        queue.enqueue(cfg, event(worker_transcript, worker_dir, session_id="worker-1"))

        report = work.run(cfg)

        assert report.digested == 2
        assert report.stopped == ""

        primary_pages = [p for p in pages(cfg) if "primary-1" in p]
        assert len(primary_pages) == 1
        fm_primary = frontmatter(cfg, f"digests/{primary_pages[0]}")
        assert "role" not in fm_primary["aft"]
        assert "role" not in fm_primary["aft"]["facts"][0]

        worker_pages = [p for p in pages(cfg) if "worker-1" in p]
        assert len(worker_pages) == 1
        fm_worker = frontmatter(cfg, f"digests/{worker_pages[0]}")
        assert fm_worker["aft"]["role"] == "worker"
        assert fm_worker["aft"]["facts"][0]["role"] == "worker"
    finally:
        fake.stop()


def test_worker_session_not_digested_when_primary_exhausts_budget(session: Session, tmp_path):
    """When primary sessions spend the daily budget, worker sessions are archived
    but not digested.
    """
    worker_dir = make_repo(tmp_path / "worker_repo").resolve()
    fake, cfg = serve(
        session.cfg,
        answer(
            said(
                statement="Primary decision made.",
                quote="Store the digest at session end",
                entry="e1",
            )
        ),
        answer(said(statement="Worker test passed.", quote="Worker ran tests", entry="w1")),
    )
    try:
        cfg = with_capture(cfg, worker_paths=[str(worker_dir)], budget=250)

        queue.enqueue(cfg, event(session.transcript, session.cwd, session_id="primary-1"))

        worker_transcript = write_records(
            tmp_path / "worker.jsonl",
            {"cwd": str(worker_dir)},
            record("w1", "user", "Worker ran tests: failed with error."),
            record("w2", "assistant", "Recorded failure.", model="claude-real"),
            record("w3", "user", WORKER_AGAIN),
            record("w4", "assistant", WORKER_REPLY, model="claude-real"),
        )
        queue.enqueue(cfg, event(worker_transcript, worker_dir, session_id="worker-1"))

        first = work.run(cfg)
        assert first.digested == 1
        assert first.stopped == work.STOP_BUDGET
        assert any("primary-1" in p for p in pages(cfg))
        assert not any("worker-1" in p for p in pages(cfg))

        assert evidence.entries(cfg, "worker-1") != []
        conn = sqlite3.connect(cfg.home / "index.sqlite")
        count = conn.execute(
            "SELECT count(*) FROM evidence WHERE session_id = 'worker-1'"
        ).fetchone()[0]
        conn.close()
        assert count > 0

        assert "worker-1" not in rows(cfg, queue.PENDING), "archiving left work waiting"
        assert owed_digest(cfg, "worker-1") is True

        # The debt is what the second run digests: no row of the worker's is waiting any more,
        # so a run that only read the queue would report an idle day and file no page.
        second = work.run(with_budget(cfg, 100_000))
        assert second.digested == 1
        assert any("worker-1" in p for p in pages(cfg))
        worker_page = [p for p in pages(cfg) if "worker-1" in p][0]
        fm_worker = frontmatter(cfg, f"digests/{worker_page}")
        assert fm_worker["aft"]["role"] == "worker"
        assert fm_worker["aft"]["facts"][0]["role"] == "worker"
        assert owed_digest(cfg, "worker-1") is False, "the digest left the debt owed"
    finally:
        fake.stop()


def _two_waiting_workers(session: Session, tmp_path) -> tuple[Config, FakeEndpoint]:
    """A config whose whole budget is spent and two worker sessions waiting in the queue."""
    directories = [make_repo(tmp_path / f"worker_{name}").resolve() for name in ("one", "two")]
    fake, cfg = serve(session.cfg)
    cfg = with_capture(cfg, worker_paths=[str(d) for d in directories], budget=0)
    for directory, name in zip(directories, ("one", "two"), strict=True):
        transcript = write_records(
            tmp_path / f"worker-{name}.jsonl",
            {"cwd": str(directory)},
            record("w1", "user", f"Worker {name} prompt: the suite is red."),
            record("w2", "user", WORKER_AGAIN),
        )
        queue.enqueue(cfg, event(transcript, directory, session_id=f"worker-{name}"))
    return cfg, fake


def test_one_worker_failing_to_archive_leaves_the_other_workers_stored(
    session: Session, tmp_path, monkeypatch
):
    """An archiving failure that belongs to one job stops that job and nothing else.

    Both workers wait behind the same spent budget, so a loop that treated any failure as
    the run's own would leave the second worker's words out of the archive while still
    reporting the budget as why it stopped (BUILD-PLAN M2.7).
    """
    cfg, fake = _two_waiting_workers(session, tmp_path)
    real = digest.archive_worker_job

    def reject_one(config: Config, job: queue.Job) -> bool:
        if job.session_id == "worker-one":
            raise digest.DigestError("this session could not be filed")
        return real(config, job)

    monkeypatch.setattr(digest, "archive_worker_job", reject_one)
    try:
        report = work.run(cfg)

        assert report.digested == 0
        assert report.stopped == work.STOP_BUDGET
        assert len(report.failures) == 1
        assert "worker-one" in report.failures[0]
        assert len(evidence.entries(cfg, "worker-two")) >= 1
        assert len(fake.requests) == 0
    finally:
        fake.stop()


def test_archiving_workers_stops_with_the_run_that_cannot_continue(
    session: Session, tmp_path, monkeypatch
):
    """An archiving failure that stops the run is recorded once, not once per worker."""
    cfg, fake = _two_waiting_workers(session, tmp_path)

    def endpoint_is_down(config: Config, job: queue.Job) -> bool:
        raise ModelError("the endpoint did not answer")

    monkeypatch.setattr(digest, "archive_worker_job", endpoint_is_down)
    try:
        report = work.run(cfg)

        assert report.stopped == work.STOP_UNREACHABLE
        assert len(report.failures) == 1
        assert evidence.entries(cfg, "worker-two") == []
    finally:
        fake.stop()


def _one_waiting_worker(session: Session, tmp_path) -> tuple[Config, FakeEndpoint]:
    """A spent budget and one worker session waiting in the queue."""
    worker_dir = make_repo(tmp_path / "worker_repo").resolve()
    fake, cfg = serve(session.cfg)
    cfg = with_capture(cfg, worker_paths=[str(worker_dir)], budget=0)
    transcript = write_records(
        tmp_path / "worker.jsonl",
        {"cwd": str(worker_dir)},
        record("w1", "user", "Worker prompt: the fixture needs a nitrile gasket."),
        record("w2", "user", WORKER_AGAIN),
    )
    queue.enqueue(cfg, event(transcript, worker_dir, session_id="worker-1"))
    return cfg, fake


def test_a_worker_whose_directory_is_gone_still_has_its_words_stored(
    session: Session, tmp_path, monkeypatch
):
    """A worker directory that will not answer as a project costs its label, not its text.

    Archiving files no page, so the project id is a label beside the words rather than a
    requirement for storing them; the directory belongs to whatever ran the session and
    goes away with it (DESIGN 8, M2.7).
    """
    cfg, fake = _one_waiting_worker(session, tmp_path)

    def no_project(*args: object, **kwargs: object) -> object:
        raise GitError("not a repository")

    monkeypatch.setattr(digest.keys, "resolve", no_project)
    try:
        report = work.run(cfg)

        assert report.failures == ()
        assert report.stopped == work.STOP_BUDGET
        archived = evidence.entries(cfg, "worker-1")
        assert any("nitrile gasket" in entry.text for entry in archived)
        assert rows(cfg, queue.DONE) == ["worker-1"]
        assert owed_digest(cfg, "worker-1") is True
        assert watermark(cfg, "worker-1") is None, "archiving moved the digest out of its place"
    finally:
        fake.stop()


def test_archiving_without_a_vault_stops_rather_than_indexing_nothing(session: Session, tmp_path):
    """Archiving names the missing vault instead of storing text no index can read.

    The walk that builds the index reads the vault, so an archive step that ran without one
    would report the worker as stored while indexing none of it (DESIGN 14).
    """
    worker_dir = make_repo(tmp_path / "worker_repo").resolve()
    cfg = with_capture(session.cfg, worker_paths=[str(worker_dir)], budget=0)
    transcript = write_records(
        tmp_path / "worker.jsonl",
        {"cwd": str(worker_dir)},
        record("w1", "user", "Worker prompt: the fixture needs a nitrile gasket."),
        record("w2", "user", WORKER_AGAIN),
    )
    job = queue.Job(
        id="job_v",
        agent=CLAUDE,
        session_id="worker-v",
        cwd=worker_dir,
        transcript_path=transcript,
        kind=SESSION_END,
        event_time=NOW,
        watermark=None,
        off_record=False,
    )
    shutil.rmtree(cfg.vault)

    with pytest.raises(vault.VaultError):
        digest.archive_worker_job(cfg, job)
    assert evidence.entries(cfg, "worker-v") == []


def test_an_archiving_failure_that_is_not_a_lost_directory_reaches_the_run(
    session: Session, tmp_path, monkeypatch
):
    """Only the failure archiving can absorb is absorbed; the rest is said out loud.

    An archiving step that caught every error to keep going would report a clean run with
    the worker's words stored under a project that never existed (BUILD-PLAN M2.7).
    """
    cfg, fake = _one_waiting_worker(session, tmp_path)

    def disk_is_gone(*args: object, **kwargs: object) -> object:
        raise OSError("the disk went away")

    monkeypatch.setattr(digest.keys, "resolve", disk_is_gone)
    try:
        report = work.run(cfg)

        assert len(report.failures) == 1
        assert "worker-1" in report.failures[0]
        assert evidence.entries(cfg, "worker-1") == []
    finally:
        fake.stop()


def test_redigesting_a_worker_session_keeps_its_role(session: Session, tmp_path):
    """A rebuilt digest is still the machine's own, not a person's.

    A worker's confirmed lines count toward no minimum, so a rebuild that filed the same
    words as a primary would let the machine appear to back what it said alongside a person
    (rules.py, BUILD-PLAN M2.7). The rebuild starts from a session id, so the role has to be
    re-derived from the directory the queue last saw that session in.
    """
    worker_dir = make_repo(tmp_path / "worker_repo").resolve()
    worker_answer = answer(
        said(statement="Worker test passed.", quote="Worker ran tests", entry="w1")
    )
    fake, cfg = serve(session.cfg, worker_answer, worker_answer)
    try:
        cfg = with_capture(cfg, worker_paths=[str(worker_dir)])
        transcript = write_records(
            tmp_path / "worker.jsonl",
            {"cwd": str(worker_dir)},
            record("w1", "user", "Worker ran tests: all green."),
            record("w2", "assistant", "Recorded test results.", model="claude-real"),
            record("w3", "user", WORKER_AGAIN),
            record("w4", "assistant", WORKER_REPLY, model="claude-real"),
        )
        queue.enqueue(cfg, event(transcript, worker_dir, session_id="worker-1"))
        assert work.run(with_budget(cfg, 100_000)).digested == 1
        written = next(p for p in pages(cfg) if "worker-1" in p)
        before = frontmatter(cfg, f"digests/{written}")
        assert before["aft"]["role"] == "worker"
        assert before["aft"]["facts"][0]["role"] == "worker"

        rebuilt = digest.redigest(cfg, "worker-1", NOW)

        after = frontmatter(cfg, rebuilt.path)
        assert after["aft"]["role"] == "worker"
        assert after["aft"]["facts"][0]["role"] == "worker"
    finally:
        fake.stop()


def test_an_excluded_session_is_refused_a_rebuild(session: Session):
    """A session under capture.exclude_paths gets no page from a rebuild either.

    The archive can hold a session captured before its directory was named in
    `exclude_paths`, and `aft redigest` is the one path that would put those words in the
    vault after the decision said not to - so it refuses before the model is asked, not
    after (BUILD-PLAN M2.7).
    """
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        queue.enqueue(cfg, event(session.transcript, session.cwd))
        assert work.run(with_budget(cfg, 100_000)).digested == 1
        assert pages(cfg) != [], "the session was never archived, so nothing is at stake"

        cfg = with_capture(cfg, exclude_paths=[str(session.cwd)])
        with pytest.raises(digest.DigestError, match="excludes"):
            digest.redigest(cfg, SESSION, NOW)

        assert len(fake.requests) == 1, "the rebuild spent a token on an excluded session"
    finally:
        fake.stop()


def test_a_run_whose_endpoint_is_down_still_archives_its_workers(session: Session, tmp_path):
    """Waiting for a model is no reason to leave a worker's words out of the archive.

    Archiving reads a transcript and writes rows; it spends no token. So an outage stops the
    digests and not the storing, which is the whole point of the worker role (M2.7).
    """
    (session.cfg.home / "config.toml").write_text(
        (session.cfg.home / "config.toml").read_text(encoding="utf-8")
        + '[model]\nendpoint = "http://127.0.0.1:9/v1"\nname = "fake-model"\n'
        + f'[capture]\nworker_paths = ["{tmp_path / "worker_repo"}"]\n[work]\n'
        + "daily_token_budget = 100000\n",
        encoding="utf-8",
    )
    cfg = configmod.load()
    worker_dir = make_repo(tmp_path / "worker_repo").resolve()
    transcript = write_records(
        tmp_path / "worker.jsonl",
        {"cwd": str(worker_dir)},
        record("w1", "user", "Worker prompt: the fixture needs a nitrile gasket."),
        record("w2", "user", WORKER_AGAIN),
    )
    queue.enqueue(cfg, event(transcript, worker_dir, session_id="worker-1"))

    report = work.run(cfg)

    assert report.stopped == work.STOP_UNREACHABLE
    assert report.digested == 0
    assert pages(cfg) == []
    assert any("nitrile gasket" in entry.text for entry in evidence.entries(cfg, "worker-1"))
    assert watermark(cfg, "worker-1") is None, "an archiving run moved the digest watermark"


def test_what_the_index_could_not_read_is_said_by_the_run(session: Session):
    """A page the index could not read is named by the run, not dropped on the floor.

    The report is a list of lines the caller has to do something with; a worker that ignored
    it would leave the same file unreadable on every run forever, with nothing on the line to
    say why one project's words never come up in a search (BUILD-PLAN M3.1).
    """
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        project_dir = cfg.vault / "projects" / SLUG
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "not-text.md").write_bytes(b"---\ntitle: \xff\xfe\n---\n\n## Facts\n\n")
        queue.enqueue(cfg, event(session.transcript, session.cwd))

        report = work.run(with_budget(cfg, 100_000))

        assert report.digested == 1, "an unreadable page stopped the digest it never touched"
        assert report.stopped == ""
        assert [line for line in report.failures if "not-text.md" in line], report.failures
    finally:
        fake.stop()


def test_capture_follows_a_worker_session_the_archive_stored(
    session: Session, tmp_path, monkeypatch
):
    """An archived worker is not a session capture stopped watching.

    The row archiving left behind used to wait in the queue, and a waiting row is capture work
    in flight, so every later sweep passed the session by and text written after the archive
    was stored by nothing at all. Archiving closes that row and owes a digest instead, and a
    debt on the session row is not capture work: the session is offered again as soon as it
    has said more (BUILD-PLAN M2.7, the lead's review of #39).
    """
    worker_dir = make_repo(tmp_path / "worker_repo").resolve()
    sessions = tmp_path / "agent-sessions"
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})
    monkeypatch.setattr(queue, "_now", lambda: NOW)
    fake, cfg = serve(session.cfg)
    try:
        cfg = with_capture(cfg, worker_paths=[str(worker_dir)], budget=0)
        transcript = write_records(
            sessions / "dev-worker" / "worker-1.jsonl",
            {"cwd": str(worker_dir)},
            record("w1", "user", "Worker prompt: the suite is red."),
            record("w2", "user", WORKER_AGAIN),
        )
        quiet = NOW - timedelta(minutes=40)
        os.utime(transcript, (quiet.timestamp(), quiet.timestamp()))
        queue.enqueue(cfg, event(transcript, worker_dir, session_id="worker-1"))

        assert work.run(cfg, NOW).stopped == work.STOP_BUDGET
        assert owed_digest(cfg, "worker-1") is True
        # The archive read the transcript through here, so the sweep has nothing to hand over
        # and re-filing the same text would be the churn this replaces.
        assert capture.sweep(cfg, NOW + timedelta(hours=2)) == []

        spoke = NOW + timedelta(hours=1)
        append_records(transcript, record("w3", "user", "Worker says the fix landed."))
        os.utime(transcript, (spoke.timestamp(), spoke.timestamp()))

        found = capture.sweep(cfg, NOW + timedelta(hours=2))
        assert [e.session_id for e in found] == ["worker-1"], "capture is still skipping it"

        assert work.run(cfg, NOW + timedelta(hours=2)).stopped == work.STOP_BUDGET
        assert any("fix landed" in entry.text for entry in evidence.entries(cfg, "worker-1")), (
            "the text written after the archive reached nothing"
        )
    finally:
        fake.stop()


def test_a_worker_that_goes_quiet_after_archiving_is_still_digested(session: Session, tmp_path):
    """Nothing has to happen in the session for its digest to arrive.

    Its row left the queue when the archive stored it, so no hook and no sweep will ever bring
    that text back: the debt is the only thing left that remembers the digest, and a run with
    room in the budget after primaries has to find it on its own (BUILD-PLAN M2.7).
    """
    worker_dir = make_repo(tmp_path / "worker_repo").resolve()
    worker_answer = answer(
        said(statement="Worker test passed.", quote="Worker ran tests", entry="w1")
    )
    fake, cfg = serve(session.cfg, worker_answer, worker_answer)
    try:
        cfg = with_capture(cfg, worker_paths=[str(worker_dir)], budget=0)
        transcript = write_records(
            tmp_path / "worker.jsonl",
            {"cwd": str(worker_dir)},
            record("w1", "user", "Worker ran tests: all green."),
            record("w2", "assistant", "Recorded test results.", model="claude-real"),
            record("w3", "user", WORKER_AGAIN),
            record("w4", "assistant", WORKER_REPLY, model="claude-real"),
        )
        queue.enqueue(cfg, event(transcript, worker_dir, session_id="worker-1"))

        assert work.run(cfg).stopped == work.STOP_BUDGET
        assert pages(cfg) == []
        assert owed_digest(cfg, "worker-1") is True

        report = work.run(with_budget(cfg, 100_000))

        assert report.digested == 1, "the run found nothing, though it owed this session"
        worker_pages = [p for p in pages(cfg) if "worker-1" in p]
        assert len(worker_pages) == 1
        fm_worker = frontmatter(cfg, f"digests/{worker_pages[0]}")
        assert fm_worker["aft"]["role"] == "worker", "the debt's digest filed a primary page"
        assert fm_worker["aft"]["facts"][0]["role"] == "worker"
        assert owed_digest(cfg, "worker-1") is False, "the digest left the debt owed"
        assert rows(cfg, queue.PENDING) == []
        assert watermark(cfg, "worker-1") is not None
    finally:
        fake.stop()


def test_a_stored_worker_makes_the_next_run_walk_the_vault_once(
    session: Session, tmp_path, monkeypatch
):
    """A worker the archive already stored is not stored again on every run.

    Its row used to wait in the queue, so each run re-read the same transcript from the
    session's watermark, re-wrote the same entries, and walked the whole vault to re-index
    them - work that grows with the vault and buys nothing (BUILD-PLAN M2.7). One walk per run
    is the run's own: a closed row leaves the archive nothing to do.
    """
    cfg, fake = _one_waiting_worker(session, tmp_path)
    walks: list[int] = []
    real = digest.index.update

    def count_the_walks(config: Config) -> list[str]:
        walks.append(1)
        return real(config)

    monkeypatch.setattr(digest.index, "update", count_the_walks)
    try:
        work.run(cfg)
        after_first = len(walks)
        assert after_first == 2, "one walk to index what was stored, one for the run's end"

        assert work.run(cfg).stopped == work.STOP_BUDGET

        assert len(walks) == after_first + 1, "a run with nothing to store re-walked the vault"
        assert owed_digest(cfg, "worker-1") is True, "and lost the digest it still owes"
    finally:
        fake.stop()


def test_a_debt_for_a_session_excluded_after_it_was_stored_is_dropped(session: Session, tmp_path):
    """A debt owed on words the vault has decided not to hold is not collected.

    Naming the worker's directory in `exclude_paths` after the fact says its words are not the
    vault's, and exclusion is enforced by leaving no trace: a waiting row or a debt naming that
    session is a trace, so the debt goes rather than filing work whose digest would only be
    refused (BUILD-PLAN M2.7).
    """
    cfg, fake = _one_waiting_worker(session, tmp_path)
    worker_dir = (tmp_path / "worker_repo").resolve()
    try:
        assert work.run(cfg).stopped == work.STOP_BUDGET
        assert owed_digest(cfg, "worker-1") is True

        cfg = with_capture(cfg, worker_paths=[str(worker_dir)], exclude_paths=[str(worker_dir)])

        assert queue.redeem_digest_debts(cfg) == 0
        assert owed_digest(cfg, "worker-1") is False
        assert rows(cfg, queue.PENDING) == []
        assert pages(cfg) == []
    finally:
        fake.stop()


# ---------------------------------------------------------------- the floor and the window


def one_prompt_transcript(session: Session, name: str) -> Path:
    """A session that never got past one prompt, which is what DESIGN 8 keeps out."""
    return write_records(
        session.transcript.with_name(name),
        {"cwd": str(session.cwd)},
        record("e1", "user", USER_PROMPT),
        record("e2", "assistant", ASSISTANT_ANSWER, model="claude-real"),
    )


def test_a_session_with_one_prompt_is_neither_archived_nor_asked(session: Session):
    """DESIGN 8 skips a session with fewer than two prompts, before anything is archived.

    Below the floor the session reaches no model, no archive, no index and no page: an
    archived session is searchable and dreamable, which is more than a single question
    earned. Its row still closes, because a row left waiting would stop capture following
    the session at all.
    """
    thin = one_prompt_transcript(session, "one-prompt.jsonl")
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        queue.enqueue(cfg, event(thin, session.cwd, session_id="thin-1"))
        report = work.run(cfg)
        asked = json.dumps(fake.requests)
        indexmod.update(cfg)
    finally:
        fake.stop()
    assert report.digested == 0
    assert asked == "[]", "a session below the two-prompt floor was asked of the model"
    assert evidence.entries(cfg, "thin-1") == [], "a thin session was archived"
    assert rows(cfg, queue.DONE) == ["thin-1"], "the thin session's row waited instead of closing"
    assert owed_digest(cfg, "thin-1") is False, "a digest is owed for words the floor kept out"
    assert pages(cfg) == []
    assert indexed_words(cfg, "per turn") == dict.fromkeys(indexmod.COLLECTIONS, 0)


def test_a_session_that_grows_past_the_floor_is_captured_from_its_start(session: Session):
    """The floor holds a thin session back; it does not consume it.

    The same session, reported again once it grew, is read from its first entry: the prompt
    it had while it was thin is archived with the rest. That is what a watermark the floor
    left alone buys - a session is never judged on the chunk that found it thin.
    """
    thin = one_prompt_transcript(session, "grew.jsonl")
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        queue.enqueue(cfg, event(thin, session.cwd, session_id="thin-1"))
        first = work.run(cfg)
        append_records(
            thin,
            record("e3", "user", SECOND_PROMPT),
            record("e4", "assistant", SECOND_ANSWER, model="claude-real"),
        )
        queue.enqueue(cfg, event(thin, session.cwd, session_id="thin-1"))
        second = work.run(cfg)
        archived = [entry.id for entry in evidence.entries(cfg, "thin-1")]
        standing = pages(cfg)
    finally:
        fake.stop()
    assert first.digested == 0
    assert second.digested == 1, "the session that grew was never read from its start"
    assert archived == ["e1", "e2", "e3", "e4"], "the floor's first prompt was lost"
    assert standing == [below("thin-1")]


def test_a_worker_session_with_one_prompt_is_not_archived(session: Session, tmp_path):
    """The same floor for a worker session, whose whole point is being archived anyway.

    A worker's words are stored without a model call, so the floor has to be enforced on the
    archiving path too. Its row still closes and no digest is owed for text no archive holds
    (BUILD-PLAN M2.7).
    """
    worker_dir = make_repo(tmp_path / "worker_repo").resolve()
    fake, cfg = serve(session.cfg)
    cfg = with_capture(cfg, worker_paths=[str(worker_dir)], budget=0)
    transcript = write_records(
        tmp_path / "worker.jsonl",
        {"cwd": str(worker_dir)},
        record("w1", "user", "Worker prompt: the fixture needs a nitrile gasket."),
    )
    queue.enqueue(cfg, event(transcript, worker_dir, session_id="worker-1"))
    try:
        report = work.run(cfg)
        archived = evidence.entries(cfg, "worker-1")
        debt = owed_digest(cfg, "worker-1")
        closed = rows(cfg, queue.DONE)
    finally:
        fake.stop()
    assert report.stopped == work.STOP_BUDGET
    assert archived == [], "a worker below the two-prompt floor was archived"
    assert debt is False, "a digest is owed for words the floor kept out"
    assert closed == ["worker-1"], "the thin worker's row waited instead of closing"


def test_a_run_purges_the_archive_past_its_window_and_the_index_forgets_it(session: Session):
    """The retention window is enforced by the run, not by a command nobody runs.

    `evidence.purge` arrived with M2.4 and no caller, so archived text outlived
    `evidence.retention_days` and stayed searchable beside it. A run now opens with the
    window, and the index update at its end drops the rows of whatever left, so no search
    finds a session the archive has forgotten (DESIGN 8).
    """
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        queue.enqueue(cfg, event(session.transcript, session.cwd))
        first = work.run(cfg)
        archived = len(evidence.entries(cfg, SESSION))
        before = indexed_words(cfg, "retry")
        later = work.run(cfg, NOW + timedelta(days=120))
        after = indexed_words(cfg, "retry")
    finally:
        fake.stop()
    assert first.digested == 1
    assert first.purged == 0, "a run purged text that was still inside its window"
    assert archived == 4
    assert before[indexmod.EVIDENCE] == 2, "the archive's own words are not searchable"
    assert later.purged == archived, "a run let past its window stay in the archive"
    assert evidence.entries(cfg, SESSION) == []
    assert later.digested == 0
    assert after == dict.fromkeys(indexmod.COLLECTIONS, 0), "the index kept what the purge left"


def test_a_run_says_the_archive_refused_its_window_and_still_digests(session: Session):
    """An archive that refuses the window it was configured with is reported, not obeyed.

    A negative window is not a window at all, and `evidence.purge` refuses it rather than
    emptying the archive. That refusal belongs on the run's lines; the day's digests are
    other work and stand on their own (DESIGN 14).
    """
    fake, cfg = serve(session.cfg, answer(said()))
    written = (cfg.home / configmod.CONFIG_NAME).read_text(encoding="utf-8")
    (cfg.home / configmod.CONFIG_NAME).write_text(
        written + "\n[evidence]\nretention_days = -1\n", encoding="utf-8"
    )
    cfg = configmod.load()
    try:
        queue.enqueue(cfg, event(session.transcript, session.cwd))
        report = work.run(cfg)
    finally:
        fake.stop()
    assert report.digested == 1, "an unusable retention window stopped the day's digests"
    assert report.purged == 0
    assert any("the archive:" in line for line in report.failures), report.failures


def test_a_project_named_in_capture_exclude_projects_is_never_captured(
    session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """`capture.exclude_projects` keeps a project's sessions out at the sweep and the hook.

    The project is the registered one of the `session` fixture, named by its slug, and the
    session is the path a missed hook arrives by: a file in the agent's own directory, quiet
    long enough to sweep. With the name taken away the same session is captured, which is
    what says the setting and not the fixture kept it out (DESIGN 12).
    """
    sessions = tmp_path / "agent-sessions"
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})
    transcript = write_records(
        sessions / SLUG / f"{SESSION}.jsonl",
        {"cwd": str(session.cwd)},
        record("e1", "user", USER_PROMPT),
        record("e2", "assistant", ASSISTANT_ANSWER, model="claude-real"),
        record("e3", "user", SECOND_PROMPT),
        record("e4", "assistant", SECOND_ANSWER, model="claude-real"),
    )
    quiet = datetime.now(UTC) - timedelta(minutes=40)
    os.utime(transcript, (quiet.timestamp(), quiet.timestamp()))
    moment = datetime.now(UTC)
    fake, cfg = serve(session.cfg, answer(said()), answer(said()))
    try:
        excluded = with_capture(cfg, exclude_projects=[SLUG])
        queue.watching_since(excluded, quiet - timedelta(hours=6))
        assert capture.sweep(excluded, moment) == [], "a named project was swept"

        queue.enqueue(excluded, event(transcript, session.cwd))
        assert rows(excluded, queue.PENDING) == [], "the hook's own report was queued"
        assert rows(excluded, queue.DONE) == []

        held = work.run(excluded)
        assert held.swept == 0 and held.digested == 0
        assert evidence.entries(excluded, SESSION) == []

        captured = with_capture(cfg)
        assert [e.session_id for e in capture.sweep(captured, moment)] == [SESSION]
        report = work.run(captured)
        archived = evidence.entries(captured, SESSION)
        standing = pages(captured)
    finally:
        fake.stop()
    assert report.digested == 1, "the session was kept out by something but the setting"
    assert len(archived) == 4
    assert standing == [below()]


def test_a_planted_secret_reaches_neither_model_archive_nor_index(session: Session):
    """One end-to-end check of DESIGN 8 and DESIGN 12: a secret stays where it was said.

    A token, a Slack token and a Google API key sit beside ordinary work and a test
    password in one prompt. After a whole `aft work` each secret is looked for in every place
    it could have landed: the body the endpoint was sent, the archive's own file, and the search
    index with its write-ahead log. The test password survives because redaction is by shape
    alone.
    """
    token = "ghp_NotARealGitHubToken0123456789"
    slack = "xoxb-not-a-real-slack-token-1234"
    google = "AIzaSyNotARealGoogleApiKey0123456789012345"
    password = "hunter2notreal"
    # The session's ordinary work carries the prompt the digest will quote, so the fact it
    # files is a verified one and the page is the normal size.
    secret_prompt = (
        USER_PROMPT
        + " Wire it with token "
        + token
        + ", then Slack "
        + slack
        + ' and {"password": "'
        + password
        + '"} and the map key " + google + " for the tiles."'
    )
    transcript = write_records(
        session.transcript.with_name("secrets.jsonl"),
        {"cwd": str(session.cwd)},
        record("e1", "user", secret_prompt),
        record("e2", "assistant", ASSISTANT_ANSWER, model="claude-real"),
        record("e3", "user", SECOND_PROMPT),
        record("e4", "assistant", SECOND_ANSWER, model="claude-real"),
    )
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        queue.enqueue(cfg, event(transcript, session.cwd))
        report = work.run(cfg)
        asked = json.dumps(fake.requests)
        archived = " ".join(entry.text for entry in evidence.entries(cfg, SESSION))
        on_disk = b"".join(
            one.read_bytes()
            for name in (evidence.EVIDENCE_NAME, indexmod.INDEX_NAME)
            for one in sorted((cfg.home).glob(f"{name}*"))
        )
        written = body(cfg, page_path())
        indexed = indexed_words(cfg, "retry")
    finally:
        fake.stop()
    assert report.digested == 1
    assert indexed[indexmod.EVIDENCE] > 0, "the session's own words are not searchable"
    for secret in (token, slack, google):
        assert secret not in asked, f"{secret} reached the model"
        assert secret not in archived, f"{secret} reached the archive"
        assert secret.encode() not in on_disk, f"{secret} reached the files on disk"
        assert secret not in written, f"{secret} reached the page"
    assert password in archived, "the test password should survive shape-only redaction"
    assert password in asked, "the test password should reach the model"
    assert REDACTED in archived, "nothing was redacted, so the zeros prove nothing"


# ---------------------------------------------------------------- memory reflected back (T14)

REMEMBERED = "Capture and injection run from agent lifecycle hooks, not from the model."

OVERVIEW_WITH_MEMORY = """---
type: Project
title: Widget
description: A widget that ships on time.
sources:
  - id: s1
    resource: aft://session/claude/s-0#e1
    quote: hooks, not the model
aft:
  project: {project_id}
---

# Widget

## Decisions

- {remembered} [stated] 2026-09-01 [^s1]
"""


def remembering(session: Session) -> None:
    """Give the widget project a curated line, so a session starting there is given it."""
    project = registry.Registry.load(session.cfg.registry_path).by_slug(SLUG)
    page_path_ = session.cfg.vault / "projects" / SLUG / "overview.md"
    page_path_.parent.mkdir(parents=True, exist_ok=True)
    page_path_.write_text(
        OVERVIEW_WITH_MEMORY.format(project_id=project.id, remembered=REMEMBERED),
        encoding="utf-8",
    )


def test_memory_lines_are_the_block_a_session_is_given(session: Session):
    remembering(session)
    resolution = keys.resolve(
        session.cwd, registry.Registry.load(session.cfg.registry_path), session.cfg.host
    )
    assert REMEMBERED in digest.memory_lines(session.cfg, resolution)


def test_lines_that_repeat_memory_are_taken_out_and_the_rest_kept():
    """A line is an echo whatever list marker or wrapping it arrives in; the entry keeps
    everything else, and an entry that held nothing but memory is gone."""
    memory = frozenset({REMEMBERED, "Files are the truth: every index is a cache."})
    # "## Decisions" is a line of every block, and too short to be taken for memory.
    entries = [
        Entry(
            "a1",
            NOW,
            "assistant",
            f"Here is what I know:\n- {REMEMBERED}\nAnd one new thing.",
            False,
        ),
        Entry("a2", NOW, "assistant", f">   {REMEMBERED.replace(' ', '   ')}", False),
        Entry("a3", NOW, "assistant", "## Decisions", False),
        Entry("u1", NOW, "user", "Capture runs at session end.", False),
    ]
    kept = digest.without_memory_echo(entries, memory)
    assert [(entry.id, entry.text) for entry in kept] == [
        ("a1", "Here is what I know:\nAnd one new thing."),
        ("a3", "## Decisions"),
        ("u1", "Capture runs at session end."),
    ]


def test_a_session_repeating_its_memory_is_not_evidence_for_it(session: Session):
    """The words a session was given at start and repeated back never reach the model, the
    archive or a digest: memory confirmed by its own echo would count as recurrence (R3)."""
    remembering(session)
    write_transcript(
        session.transcript,
        session.cwd,
        record("e6", "assistant", f"From memory:\n- {REMEMBERED}", model="claude-real"),
    )
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
        sent = json.dumps(fake.requests)
    finally:
        fake.stop()
    assert wrote is not None
    assert "From memory" in sent, "the rest of the entry still reaches the model"
    assert REMEMBERED not in sent
    archived = evidence.entries(cfg, SESSION)
    assert all(REMEMBERED not in entry.text for entry in archived)
    assert [entry.text for entry in archived if entry.id == "e6"] == ["From memory:"]


def prompt_words() -> str:
    """The prompt with its line breaks folded, so a test reads sentences, not wrapping."""
    return " ".join(digest.prompt_text().split())


def test_the_digest_prompt_says_restated_memory_is_not_a_fact():
    assert "A fact whose only support is that memory being restated" in prompt_words()


def test_the_digest_prompt_narrows_stated_to_the_users_own_assertions():
    """DESIGN 6: requests, questions and pasted text are never [stated]."""
    words = prompt_words()
    assert "stated: the user asserted it in their own words" in words
    assert 'A request for the task at hand ("run the tests") is not a fact at all.' in words
    assert "Text the user pasted" in words and "is not the user's own words" in words


def test_the_digest_prompt_sends_session_progress_to_the_handoff():
    """DESIGN 6: what a session did is no fact; the summary and handoff carry it."""
    assert (
        "where the work stood goes in the handoff, which only the next session "
        "sees as Last session" in prompt_words()
    )


def test_the_digest_prompt_gives_every_fact_one_value_test():
    """F46: a fact earns memory only when a future session would stumble or re-ask."""
    assert "a future session would make a mistake, or ask the user again, without it" in (
        prompt_words()
    )


def test_the_digest_prompt_names_what_is_never_a_fact():
    """F46: progress and repository facts are never facts, long or short term."""
    words = prompt_words()
    assert "Never a fact, long or short term" in words
    for item in (
        "the status of a pull request, task, branch or commit",
        "who is working on what",
        "a test's name or result",
        "a review's outcome",
        "what a change or fix did to the code",
        "anything the repository's own documents already say",
    ):
        assert item in words


def test_the_digest_prompt_defines_short_term_as_a_temporary_condition():
    """F46: short term is a condition that changes how an agent acts, never progress."""
    words = prompt_words()
    assert "a temporary condition that changes how an agent should act" in words
    assert "(something paused, a temporary setting, a freeze), not progress on the work" in words
    assert "such as who is working on what, what is paused, or a temporary setting" not in words


def test_the_digest_prompt_defines_both_terms_and_defaults_to_short():
    words = prompt_words()
    assert (
        "long: it will still be true and useful the next time someone opens this project" in words
    )
    assert "When unsure, use short." in words


@pytest.mark.parametrize(
    ("fact", "reason"),
    [
        (said(source="inferred", term="short"), "not [inferred]"),
        (said(source="suggested", term="short"), "not [suggested]"),
        (said(term="short", scope="global", kind="preference"), "never global"),
    ],
)
def test_a_short_term_fact_outside_what_short_term_holds_is_dropped(
    session: Session, fact: dict, reason: str
):
    """DESIGN 8: short-term memory holds only what the user said or a session saw, and only
    for a project; anything else is refused with its reason on the page."""
    wrote = digest_once(session, answer(fact))
    aft = frontmatter(session.cfg, wrote.path)["aft"]
    assert "facts" not in aft
    assert [d["statement"] for d in aft["dropped"]] == [fact["statement"]]
    assert reason in aft["dropped"][0]["reason"]


def test_a_long_and_a_short_fact_are_written_with_their_terms(session: Session):
    wrote = digest_once(
        session,
        answer(
            said(),
            said(statement="The hook rewrite is paused.", source="observed", term="short"),
        ),
    )
    kept = frontmatter(session.cfg, wrote.path)["aft"]["facts"]
    assert [(f["statement"], f["term"]) for f in kept] == [
        ("Capture runs at session end, not per turn.", "long"),
        ("The hook rewrite is paused.", "short"),
    ]
    assert "The hook rewrite is paused. [observed]" in body(session.cfg, wrote.path)


def test_a_fact_recorded_without_a_term_reads_as_long():
    """Library and memory-import digests carry no term (DESIGN 6)."""
    assert digest.term_of({"statement": "s"}) == digest.LONG
    assert digest.term_of({"statement": "s", "term": "short"}) == digest.SHORT


def test_a_worker_repeating_its_memory_archives_no_echo(session: Session):
    """Worker sessions are archived without a digest, and search reads that archive, so the
    echo is taken out on that path too; an entry that was nothing but memory is not stored."""
    remembering(session)
    write_transcript(
        session.transcript,
        session.cwd,
        record("e6", "assistant", f"- {REMEMBERED}", model="claude-real"),
    )
    cfg = with_capture(session.cfg, worker_paths=[str(session.cwd)])
    queue.enqueue(cfg, event(session.transcript, session.cwd))
    [job] = queue.pending_jobs(cfg, role="worker")
    assert digest.archive_worker_job(cfg, job)
    archived = evidence.entries(cfg, SESSION)
    assert archived, "the worker's other words are archived"
    assert all(REMEMBERED not in entry.text for entry in archived)
    assert "e6" not in [entry.id for entry in archived]


def test_the_digest_prompt_names_no_one_and_guesses_no_pronoun():
    """Memory lines say "the user" and "the agent", never a name or a guessed pronoun."""
    words = prompt_words()
    assert 'the summary and handoff as much as each fact, calls the user "the user"' in words
    assert "never by name, and never with he, she or another pronoun" in words


def test_a_digest_of_an_unregistered_directory_carries_its_path(initialized: Config):
    """A digest of an unregistered directory carries the path its sessions ran in as
    ``aft.path`` (DESIGN 7, F49), so the index files its facts into that directory's own
    scope and not into whatever directory once claimed the name the digest is filed under.
    A digest of a registered project carries the project's id, and an unfiled one carries
    neither, being nobody's project."""
    import datetime as dt

    cfg = initialized
    registered = registry.Registry.load(cfg.registry_path).add("widget")
    when = dt.date(2026, 10, 5)
    stamp = dt.datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    handoff = {"in_flight": [], "failed": [], "open": []}

    def frontmatter(resolution: keys.Resolution) -> dict:
        return digest._frontmatter(
            cfg,
            resolution,
            "widget",
            [],
            handoff,
            [],
            [],
            when,
            stamp,
        )["aft"]

    in_project = frontmatter(keys.Resolution(keys.PATH, cfg.vault, registered, "widget", cfg.vault))
    assert in_project["project"] == registered.id
    assert "path" not in in_project

    unregistered = frontmatter(
        keys.Resolution(keys.UNREGISTERED, cfg.vault, None, "widget", cfg.vault / "notes")
    )
    assert "project" not in unregistered
    assert unregistered["path"] == str(cfg.vault / "notes")

    unfiled = frontmatter(keys.Resolution(keys.NONE, cfg.vault))
    assert "project" not in unfiled and "path" not in unfiled
