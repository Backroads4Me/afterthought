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
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
    work,
)
from aft import config as configmod
from aft.config import Config
from aft.events import SESSION_END, SessionEvent
from aft.readers import CLAUDE, PI, Entry
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


def write_transcript(path: Path, cwd: Path, *extra: dict) -> Path:
    """A Claude Code session file: a prompt, an answer, and a tool result in the prompt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
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
        *extra,
    ]
    with path.open("w", encoding="utf-8") as handle:
        for one in records:
            handle.write(json.dumps(one) + "\n")
    return path


def record(entry_id: str, role: str, text: str, model: str | None = None) -> dict:
    """One conversation record of a Claude Code transcript."""
    message: dict = {"content": [{"type": "text", "text": text}]}
    if model:
        message["model"] = model
    return {"type": role, "uuid": entry_id, "timestamp": NOW.isoformat(), "message": message}


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
) -> dict:
    """One fact of the shape the schema describes, citing a real entry of the transcript."""
    return {
        "statement": statement,
        "kind": kind,
        "source": source,
        "quote": quote,
        "entry": entry,
        "scope": scope,
    }


def answer(*facts: dict, summary: str = "Built the capture hook.", in_flight: str = "") -> dict:
    """One digest answer of the shape the schema describes."""
    return {
        "summary": summary,
        "handoff": {"in_flight": in_flight, "failed": [], "open": []},
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


def head(cfg: Config) -> str:
    """What the vault's own HEAD commit says, subject line and file list."""
    return subprocess.run(
        ["git", "log", "--name-only", "-1", "--format=%s"],
        cwd=cfg.vault,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def page_path(session_id: str = SESSION) -> str:
    """Where a digest of ``NOW`` is filed, per DESIGN 6."""
    return f"digests/{below(session_id)}"


def below(session_id: str = SESSION) -> str:
    """The same path as `pages` reports it: below the ``digests`` directory itself."""
    return f"{SLUG}/{NOW:%Y}/{NOW:%m}/{NOW:%Y-%m-%d}-{CLAUDE}-{session_id}.md"


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
    """R2: what the agent declared is not a commitment by Ted, however decisive it reads."""
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
        == "[stated] cites entry e2, which is not one of Ted's turns"
    )


def test_stated_on_a_users_own_entry_is_kept(session: Session):
    """The other half of R2: Ted's own commitment stands, or the rule empties memory."""
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
    entries = filtermod.filter_entries(list(read(CLAUDE, session.transcript, None)), cfg)
    kept, dropped = digest.verify(cfg, SESSION, entries, [said(**broken)])
    assert kept == []
    assert [fact.reason for fact in dropped] == [reason]


def test_an_entry_the_chunk_never_held_is_dropped(session: Session):
    """An id the model invents has no role to check and no text to quote."""
    wrote = digest_once(session, answer(said(entry="e99")))
    assert wrote.facts == ()
    assert (
        frontmatter(session.cfg, wrote.path)["aft"]["dropped"][0]["reason"]
        == "entry e99 is not an entry of this chunk"
    )


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
    """Not merely parses: `aft lint` reads the page and has nothing to report."""
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


def test_the_injection_reads_the_handoff_off_the_digest(session: Session):
    """The M1.2 contract: the digest this milestone writes is what a session starts with."""
    digest_once(session, answer(said(), in_flight="the sweep, still to write"))
    resolution = keys.resolve(
        session.cwd, registry.Registry.load(session.cfg.registry_path), session.cfg.host
    )
    block = inject.build(session.cfg, resolution)
    assert "## Last session" in block
    assert "- In flight: the sweep, still to write" in block


def test_a_fact_for_every_project_says_it_had_nowhere_to_go(session: Session):
    """The plan asks for a scope the page format has no field for: losing it is loud.

    The page format is the plan author's to settle, so a global fact is not relabelled as
    one project's. Each one is its own line of the dropped record, naming what it said, the
    scope it carried and the entry it came from, so "the model found nothing global" and
    "there was nowhere to write it" read apart.
    """
    wrote = digest_once(
        session,
        answer(
            said(scope="global"),
            said(statement="Ted prefers a plain hyphen.", quote="session end", scope="global"),
        ),
    )
    assert wrote.facts == ()
    dropped = frontmatter(session.cfg, wrote.path)["aft"]["dropped"]
    assert [fact["statement"] for fact in dropped] == [
        "Capture runs at session end, not per turn.",
        "Ted prefers a plain hyphen.",
    ]
    assert [fact["entry"] for fact in dropped] == ["e1", "e1"]
    assert [fact["reason"] for fact in dropped] == [
        "scope global has no field on a digest page (DESIGN 6 line grammar)",
    ] * 2
    assert "Ted prefers a plain hyphen" not in body(session.cfg, wrote.path)


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
    """A page git never took is a page the watermark does not know: it goes back out."""
    fake, cfg = serve(session.cfg, answer(said()))
    shutil.rmtree(cfg.vault / ".git")
    try:
        with pytest.raises(Exception) as raised:
            digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
        assert raised.type.__name__ in {"GitError", "VaultError"}
    finally:
        fake.stop()
    assert not (cfg.vault / page_path()).exists()
    assert pages(cfg) == []
    assert watermark(cfg) is None


def test_the_watermark_moves_only_with_the_page_and_its_commit(session: Session):
    """Success looks like this: the page, its commit in the vault's own history, the mark."""
    wrote = digest_once(session, answer(said()))
    assert (session.cfg.vault / wrote.path).is_file()
    committed = head(session.cfg).splitlines()
    assert committed[0] == f"aft: digest {wrote.path}"
    assert committed[-1] == wrote.path
    # The last entry read, tool result included, not the last one kept: what the filter
    # threw away has been decided already and must not be decided again.
    assert wrote.watermark == watermark(session.cfg) == "e3#t1"
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


def test_a_second_chunk_the_same_day_names_the_page_that_stands(session: Session):
    """Two chunks of one session, one day: the standing digest is refused, not replaced.

    The plan gives a session one digest path per day, so the second chunk has no page of
    its own to write. Overwriting the first digest would lose the earlier half of the day
    quietly; the job fails instead, and says why, with the chunk archived and the mark
    left where the first digest put it. The page's path is known before a token is spent
    on it, so a chunk that could never be filed is refused before the endpoint is called.
    """
    first = digest_once(session, answer(said()))
    grown = write_transcript(
        session.transcript.with_name("grown.jsonl"),
        session.cwd,
        record("e4", "user", "And keep the archive compressed."),
    )
    fake, cfg = serve(
        session.cfg, answer(said(statement="The archive stays compressed.", quote="archive"))
    )
    try:
        with pytest.raises(digest.DigestError) as raised:
            digest.digest_job(cfg, claim(cfg, grown, session.cwd))
    finally:
        fake.stop()
    assert page_path() in str(raised.value)
    assert fake.requests == [], "a chunk with no page to write was digested anyway"
    assert "one digest path per day" in str(raised.value)
    assert watermark(cfg) == first.watermark
    assert pages(cfg) == [below()]
    assert evidence.find_quote(cfg, SESSION, "e4", "keep the archive compressed")


def test_a_directory_that_is_no_project_at_all_is_never_sent_to_the_model(
    session: Session, tmp_path
):
    """No project, no slug: the job is refused before the chunk is read or sent.

    DESIGN 7 draws the line at a directory that belongs to no project. A page filed
    under a name nothing resolves to would be memory no session ever asks for, and the
    model would have been asked to write it for free.
    """
    nowhere = (tmp_path / "just-a-directory").resolve()
    nowhere.mkdir()
    fake, cfg = serve(session.cfg, answer(said()))
    try:
        with pytest.raises(digest.DigestError) as raised:
            digest.digest_job(cfg, claim(cfg, session.transcript, nowhere))
    finally:
        fake.stop()
    assert "no project" in str(raised.value)
    assert fake.requests == [], "a session with nowhere to be filed was digested anyway"
    assert pages(cfg) == []
    assert watermark(cfg) is None


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


def test_a_transcript_that_vanished_fails_its_job_and_the_run_moves_on(session: Session):
    """A session end whose file is gone is that job's failure, told with the path.

    The queue keeps the path the hook reported, and a run that reached it later finds it
    deleted or unmounted. That is one session's loss, not the queue's, and the reason a
    person needs is the path that was not there.
    """
    vanished = session.transcript.with_name("gone.jsonl")
    shutil.copy(session.transcript, vanished)
    good = session.transcript.with_name("still-here.jsonl")
    shutil.copy(session.transcript, good)
    fake, cfg = serve(session.cfg, answer(said()), answer(said()))
    try:
        queue.enqueue(cfg, event(vanished, session.cwd, session_id="gone-1"))
        vanished.unlink()
        queue.enqueue(cfg, event(good, session.cwd, session_id="here-1"))
        report = work.run(cfg)
    finally:
        fake.stop()
    assert report.digested == 1
    assert len(report.failures) == 1
    assert "gone.jsonl" in report.failures[0]
    assert rows(cfg, queue.FAILED) == ["gone-1"]
    assert rows(cfg, queue.DONE) == ["here-1"]
    assert watermark(cfg, "gone-1") is None


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


def long_transcript(name: str, session: Session, turns: int = 120) -> Path:
    """A session long enough that its chunk passes the 30,000-token cap.

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
    ids = [found for call in sent for found in re.findall(r"^\[(e\d+)\] ", call, re.MULTILINE)]
    assert ids == [f"e{index}" for index in range(1, 121)], "an entry was lost, cut or repeated"
    for call in sent:
        for index in range(1, 121):
            # An entry's words are all in the one call that names it, or all in neither.
            assert call.count(turn(index)) == call.count(f"[e{index}] "), f"e{index} was cut"


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
        queue.record_sweep(cfg, quiet - timedelta(hours=6))
        report = work.run(cfg)
    finally:
        fake.stop()
    assert report.swept == 1, "the session whose hook never fired was not swept"
    assert report.digested == 1
    assert len(pages(cfg)) == 1


def test_a_refused_answer_fails_its_own_job_and_the_run_carries_on(session: Session):
    """One session's unusable answer is that session's loss, not the queue's wedged.

    The plan's schema carries seven kinds and the model answers with an eighth, twice, so
    the answer is refused. DESIGN 14 assigns malformed output a per-job outcome: the row is
    left behind FAILED with the model's own reason and its watermark unmoved, the next
    session is digested in the same run, and a run after that still digests.
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
    assert marks == {"s-0": "e3#t1", "s-1": None, "s-2": None}
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
    assert fact["required"] == ["statement", "kind", "source", "quote", "entry", "scope"]
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
    assert handoff["properties"]["in_flight"]["maxLength"] == 400
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
                "handoff": {"in_flight": "", "failed": [], "open": []},
                "facts": [],
            },
            digest.digest_schema(),
        )
