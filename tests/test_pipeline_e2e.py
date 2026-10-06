"""One pipeline, end to end: two agent sessions in, curated memory out, one session forgotten.

Every other module in this suite is read from one side. The digest tests stop at the page a
digest wrote, the dream tests start from a digest written by hand, the forget tests start from a
vault someone seeded. This file asks the question the design is really answering: does a session
a person actually had, in two different agents, arrive as a line of active memory citing what
they said, without its secrets - and can one of those sessions be taken back out with nothing
left to be found?

The stages are deliberately chained inside one test rather than one test per stage: what is worth
catching is a stage handing the next one something it was not built to receive. The one
expectation this run could not meet is split out and marked, with the reason beside it.

Everything runs against a temporary home and vault, a temporary git identity and the fake
endpoint of M2.5: no model, no network, no real memory (BUILD-PLAN 1.5, DESIGN 15).
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aft import capture, cli, evidence, page, queue, search, work
from aft import config as configmod
from aft import index as indexmod
from aft.config import Config
from aft.dream import schedule
from aft.readers import CLAUDE, PI
from aft.redact import REDACTED
from conftest import make_repo, run_git
from fakes.model_server import FakeEndpoint

# The Claude session is one day, the pi session the next: a superseding fact needs a quote dated
# after the line it replaces (R4), and the quiet period is what makes a project ready to dream.
CAPTURED_AT = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
PI_AT = datetime(2026, 10, 6, 11, 0, tzinfo=UTC)
DREAMED_AT = PI_AT + timedelta(hours=3)
DREAM_DAY = "2026-10-06"
WORKED_AGAIN_AT = PI_AT + timedelta(days=1, minutes=5)

SLUG = "widget"
CLAUDE_SESSION = "e2e-claude-1"
PI_SESSION = "e2e-pi-1"

# The Claude session states a decision the pipeline has to carry all the way to an active line,
# quote intact. The pi session, a day later, says that decision was wrong, which is how a line
# gets struck out and superseded. The secrets are what neither session may leave a trace of,
# anywhere.
CLAUDE_DECISION = "The queue should keep its state in a daemon process, not a file."
CLAUDE_DECISION_QUOTE = "keep its state in a daemon process"
CLAUDE_STATEMENT = "The queue keeps its state in a daemon process."
PI_DECISION = "Forget the daemon: the queue keeps its state in the SQLite file at state/q.sqlite."
PI_DECISION_QUOTE = "the queue keeps its state in the SQLite file"
PI_STATEMENT = "The queue keeps its state in the SQLite file at state/q.sqlite."
CLAUDE_SECRET = "sk-proj-VkQ7mR2nT9xL4dF6hJ8s"
PI_SECRET = "ghp_Wrangler7calibrate01234567"

# A fact the model invents: words no entry of that session holds. The digest is meant to keep
# it off the page as a fact and name it as dropped (DESIGN 8).
BOGUS_STATEMENT = "The retry sleeps 250ms between attempts."
BOGUS_QUOTE = "the retry sleeps a quarter second"

# One sentence per session that the other does not say, so forgetting one can be measured. The
# statements above are aft's own wording and deliberately not a substring of these prompts.
CLAUDE_ONLY = "scratchpad cleanup runs before the hook returns"
PI_ONLY = "the pi extension posts its summary at session shutdown"


# ------------------------------------------------------------ what the pipeline runs over


def claude_transcript(path: Path, cwd: Path) -> Path:
    """One Claude Code session: two prompts, a decision said out loud, and a secret pasted in."""
    return write_records(
        path,
        [
            {"cwd": str(cwd)},
            claude_record("c1", "user", CLAUDE_DECISION),
            claude_record(
                "c2", "assistant", "Understood: state in a daemon, not a file.", "claude-real"
            ),
            claude_record("c3", "user", f"{CLAUDE_ONLY}, and {CLAUDE_SECRET} is the deploy key."),
            claude_record("c4", "assistant", "Done: cleanup runs first.", "claude-real"),
        ],
    )


def pi_transcript(path: Path, cwd: Path) -> Path:
    """The same shape of session in pi's own record format, the day after."""
    return write_records(
        path,
        [
            {
                "type": "session",
                "version": 3,
                "id": PI_SESSION,
                "timestamp": PI_AT.isoformat(),
                "cwd": str(cwd),
            },
            pi_record("p1", None, "user", PI_DECISION),
            pi_record("p2", "p1", "assistant", "Agreed: the file is the state, no daemon running."),
            pi_record(
                "p3",
                "p2",
                "user",
                f"{PI_ONLY}. The reader token is {PI_SECRET}.",
            ),
            pi_record("p4", "p3", "assistant", "Done: noted both."),
        ],
    )


def claude_record(entry_id: str, role: str, text: str, model: str | None = None) -> dict:
    message: dict = {"content": [{"type": "text", "text": text}]}
    if model:
        message["model"] = model
    return {
        "type": role,
        "uuid": entry_id,
        "timestamp": CAPTURED_AT.isoformat(),
        "message": message,
    }


def pi_record(entry_id: str, parent: str | None, role: str, text: str) -> dict:
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent,
        "timestamp": PI_AT.isoformat(),
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def write_records(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for one in records:
            handle.write(json.dumps(one) + "\n")
    return path


def age(path: Path, when: datetime) -> None:
    """A transcript whose clock says it stopped being written at ``when``."""
    os.utime(path, (when.timestamp(), when.timestamp()))


def hook(agent: str, event: str, payload: dict, monkeypatch) -> int:
    """`aft hook <agent> <event>` with this payload on stdin, as the agent itself runs it."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return cli.main(["hook", agent, event])


def said(statement: str, quote: str, entry: str) -> dict:
    """One fact of the shape the digest schema describes, citing one entry of the session."""
    return {
        "statement": statement,
        "kind": "decision",
        "source": "stated",
        "term": "long",
        "quote": quote,
        "entry": entry,
        "scope": "project",
    }


def digest_reply(*facts: dict) -> dict:
    """One digest answer of the shape the schema describes, carrying these facts."""
    return {
        "summary": "Chose where the queue keeps its state.",
        "handoff": {"in_flight": {"text": "", "entry": ""}, "failed": [], "open": []},
        "facts": list(facts),
    }


class Pipeline:
    """The temporary home, vault and endpoint, and the two sessions sitting in them."""

    def __init__(self, cfg: Config, endpoint: FakeEndpoint, repo: Path) -> None:
        self.cfg = cfg
        self.endpoint = endpoint
        self.endpoints = [endpoint]
        self.repo = repo
        self.transcripts = []
        self._dream: schedule.Run | None = None

    # -------------------------------------------------- the endpoint each stage is scripted on

    def script(self, *replies: object) -> None:
        """Give the next stage an endpoint scripted from its own first call.

        The fake numbers calls for its whole life, while a stage's script is written from that
        stage's first call, so each stage gets an endpoint of its own. The endpoint it retires
        stays in `endpoints`: what was sent to a model is part of what the checks read.
        """
        self.endpoint.stop()
        self.endpoint = FakeEndpoint(list(replies))
        self.endpoint.start()
        self.endpoints.append(self.endpoint)
        (self.cfg.home / configmod.CONFIG_NAME).write_text(
            f'vault = "{self.cfg.vault}"\nhost = "{self.cfg.host}"\n'
            f'[model]\nendpoint = "{self.endpoint.endpoint}"\nname = "fake-model"\n'
            # Base-pipeline answers: the turns pipeline's are served in test_digest_turns.py.
            '[digest]\npipeline = "base"\n'
        )
        self.cfg = configmod.load()

    def dream(self) -> schedule.Run:
        """`aft dream`, run once: the first stated decision is added, the second supersedes it."""
        if self._dream is None:
            self.script(
                {"action": "add", "line": None, "reason": "a decision you stated yourself"},
                {
                    "action": "supersede",
                    "line": "L1",
                    "reason": "the later session dropped the daemon",
                },
            )
            with queue._writing(self.cfg) as conn:
                conn.execute("UPDATE jobs SET event_time = ?", (PI_AT.isoformat(),))
            self._dream = schedule.run(self.cfg, now=DREAMED_AT)
        return self._dream

    # ------------------------------------------------------------------- what it left behind

    @property
    def overview(self) -> Path:
        return self.cfg.vault / "projects" / SLUG / "overview.md"

    def memory(self) -> tuple[dict, page.Page]:
        """The project's own page: its frontmatter and the lines it holds."""
        text = self.overview.read_text(encoding="utf-8")
        frontmatter, _, _, error = page.split_frontmatter(text)
        assert error is None, error
        assert frontmatter is not None
        return frontmatter, page.parse(text, self.overview)

    def commits(self) -> list[str]:
        """Every commit message in the vault, newest first."""
        return run_git(["log", "--format=%s"], self.cfg.vault).splitlines()

    def report_text(self) -> str:
        return (self.cfg.vault / "reports" / "2026" / "10" / f"{DREAM_DAY}.md").read_text(
            encoding="utf-8"
        )

    def vault_text(self) -> str:
        """Every byte of the vault's working tree, git aside."""
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(self.cfg.vault.rglob("*"))
            if path.is_file() and ".git" not in path.parts
        )

    def index_text(self) -> str:
        """The text the search index holds, in every collection it maintains."""
        if not (self.cfg.home / indexmod.INDEX_NAME).is_file():
            return ""
        columns = ", ".join(indexmod.INDEXED_COLUMNS)
        parts: list[str] = []
        with sqlite3.connect(self.cfg.home / indexmod.INDEX_NAME) as conn:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            for name in indexmod.COLLECTIONS:
                if name not in tables:
                    continue
                rows = conn.execute(f"SELECT {columns} FROM {name}").fetchall()
                parts.extend(" ".join(str(column) for column in row) for row in rows)
        return "\n".join(parts)

    def store_text(self, name: str) -> str:
        """One of the home's SQLite stores, write-ahead log and journal included."""
        parts = []
        for suffix in ("", "-wal", "-shm", "-journal"):
            store = self.cfg.home / (name + suffix)
            if store.is_file():
                parts.append(store.read_bytes().decode("utf-8", "replace"))
        return "\n".join(parts)

    def evidence_text(self) -> str:
        """The evidence archive as its file actually stands on disk."""
        return self.store_text(evidence.EVIDENCE_NAME)

    def everything(self) -> str:
        """Every byte afterthought controls: the vault, its stores, and the model's inbox."""
        parts = [self.vault_text(), self.index_text()]
        for name in (evidence.EVIDENCE_NAME, indexmod.INDEX_NAME, queue.QUEUE_NAME):
            parts.append(self.store_text(name))
        for endpoint in self.endpoints:
            parts.append(json.dumps(endpoint.requests))
        return "\n".join(parts)

    def words_of(self, session_id: str) -> list[str]:
        """What one session said, in its own words, as the archive holds them."""
        return [entry.text for entry in evidence.entries(self.cfg, session_id)]


@pytest.fixture
def pipeline(initialized, home, tmp_path, monkeypatch) -> Pipeline:
    """Steps 1 and 2: register a project, run both capture hooks, and `aft work` after each.

    The endpoint is scripted with what a digest may say about each session, so the facts that
    reach the vault are the model's, not the test's.
    """
    repo = make_repo(tmp_path / "dev" / SLUG).resolve()
    assert cli.main(["project", "add", str(repo)]) == 0

    claude_dir = tmp_path / "claude-sessions" / f"--{repo.name}--"
    pi_dir = tmp_path / "pi-sessions" / f"--{repo.name}--"
    claude_path = claude_transcript(claude_dir / f"{CLAUDE_SESSION}.jsonl", repo)
    pi_path = pi_transcript(pi_dir / f"2026-10-06T11-00-00-000Z_{PI_SESSION}.jsonl", repo)

    # The sweep reads the agents' own directories; here they are these two, so no run ever
    # looks at a real ~/.claude or ~/.pi.
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: claude_dir.parent, PI: pi_dir.parent})

    # The Claude answer carries one fact whose quote the session does not contain: the pipeline
    # is supposed to notice, and say so on the page rather than file the claim as memory.
    fake = FakeEndpoint(
        [
            digest_reply(
                said(CLAUDE_STATEMENT, CLAUDE_DECISION_QUOTE, "c1"),
                said(BOGUS_STATEMENT, BOGUS_QUOTE, "c1"),
            ),
            digest_reply(said(PI_STATEMENT, PI_DECISION_QUOTE, "p1")),
        ]
    )
    fake.start()

    pipe = Pipeline(configmod.load(), fake, repo)
    pipe.transcripts = [claude_path, pi_path]
    (home / configmod.CONFIG_NAME).write_text(
        f'vault = "{initialized.vault}"\nhost = "{initialized.host}"\n'
        f'[model]\nendpoint = "{fake.endpoint}"\nname = "fake-model"\n'
        # Base-pipeline answers: the turns pipeline's are served in test_digest_turns.py.
        '[digest]\npipeline = "base"\n'
    )
    cfg = pipe.cfg = configmod.load()

    assert (
        hook(
            "claude",
            "SessionEnd",
            {
                "session_id": CLAUDE_SESSION,
                "transcript_path": str(claude_path),
                "cwd": str(repo),
                "reason": "other",
            },
            monkeypatch,
        )
        == 0
    )
    # A session is worked the moment it ends, as the hook's own early trigger has it.
    first = work.run(cfg, now=CAPTURED_AT + timedelta(minutes=5))
    assert first.failures == (), first.failures
    assert first.digested == 1, first.line()

    assert (
        hook(
            "pi",
            "session_shutdown",
            {
                "session_id": PI_SESSION,
                "transcript_path": str(pi_path),
                "cwd": str(repo),
                "reason": "quit",
            },
            monkeypatch,
        )
        == 0
    )
    second = work.run(cfg, now=PI_AT + timedelta(minutes=5))
    assert second.failures == (), second.failures
    assert second.digested == 1, second.line()

    yield pipe
    pipe.endpoint.stop()
    for endpoint in pipe.endpoints[:-1]:
        endpoint.stop()


# ---------------------------------------------------------------------- the pipeline itself


def test_the_pipeline_end_to_end(pipeline: Pipeline, capsys, monkeypatch) -> None:
    """Two sessions, one dream, one injection, one search, one forgetting: the whole design."""
    cfg = pipeline.cfg

    # -- The capture hooks and `aft work` left two digests whose quotes verify, and no secret
    #    anywhere.
    digests = sorted(str(p.relative_to(cfg.vault)) for p in (cfg.vault / "digests").rglob("*.md"))
    assert len(digests) == 2, digests
    cited = 0
    for rel in digests:
        frontmatter, body, _, error = page.split_frontmatter((cfg.vault / rel).read_text())
        assert error is None, error
        assert BOGUS_STATEMENT not in body, "a claim no entry supports reached a page as a fact"
        for source in frontmatter["sources"]:
            resource = str(source["resource"])
            assert resource.startswith("aft://session/"), resource
            session, _, entry = resource.rsplit("/", 1)[-1].partition("#")
            assert evidence.find_quote(cfg, session, entry, str(source["quote"])), resource
            cited += 1
    assert cited == 2, "a digest cited no quote at all"

    # and it is named as dropped on the page that turned it down, so the loss is legible.
    claude_digest, _, _, _ = page.split_frontmatter(
        next((cfg.vault / "digests").rglob(f"*{CLAUDE_SESSION}*")).read_text()
    )
    dropped = [str(fact["statement"]) for fact in claude_digest["aft"].get("dropped") or []]
    assert dropped == [BOGUS_STATEMENT], claude_digest["aft"]

    assert CLAUDE_SECRET in pipeline.transcripts[0].read_text()
    assert PI_SECRET in pipeline.transcripts[1].read_text()
    everywhere = pipeline.everything()
    assert CLAUDE_SECRET not in everywhere
    assert PI_SECRET not in everywhere
    # The words around a secret survived, which is what says it was redacted rather than the
    # whole turn dropped.
    for session_id in (CLAUDE_SESSION, PI_SESSION):
        texts = pipeline.words_of(session_id)
        assert texts, session_id
        assert any(REDACTED in text for text in texts), session_id

    # -- `aft dream`: the stated decision becomes an active line citing its source, the later
    #    one supersedes it, one commit does the memory, and the report says what happened.
    commits_before = pipeline.commits()
    run = pipeline.dream()
    assert run.ran == [SLUG], run
    assert run.failures == {}, run.failures

    frontmatter, memory = pipeline.memory()
    active = [fact for fact in memory.facts if fact.state == page.ACTIVE]
    superseded = [fact for fact in memory.facts if fact.state == page.SUPERSEDED]
    assert [fact.statement for fact in active] == [PI_STATEMENT]
    assert [fact.statement for fact in superseded] == [CLAUDE_STATEMENT]
    assert superseded[0].struck

    sources = {str(source["id"]): source for source in frontmatter["sources"]}
    line = active[0]
    assert line.refs, f"{line.statement} carries no source"
    assert all(PI_SESSION in str(sources[ref]["resource"]) for ref in line.refs)
    quote = str(sources[line.refs[0]]["quote"])
    assert quote in PI_DECISION, quote
    assert evidence.find_quote(cfg, PI_SESSION, "p1", quote)

    after = pipeline.commits()
    new_commits = after[: len(after) - len(commits_before)]
    # One commit changed the memory, and only the report page rode in beside it.
    assert new_commits == [f"aft: report {SLUG}", f"aft: dream {SLUG}"], new_commits
    assert run.reports[SLUG].commit is not None

    report = pipeline.report_text()
    assert f"# {SLUG} {DREAMED_AT.strftime('%H:%M:%S')}Z" in report
    assert "- Added: 1" in report
    assert "- Superseded: 1" in report
    assert CLAUDE_STATEMENT in report, "the report does not name what it superseded"

    # -- `aft inject` for the project shows the line, and none of the report about it.
    assert cli.main(["inject", str(pipeline.repo)]) == 0
    block = capsys.readouterr().out
    assert PI_STATEMENT in block
    assert CLAUDE_STATEMENT not in block, "a superseded line was injected as memory"
    assert f"# {SLUG} {DREAMED_AT.strftime('%H:%M:%S')}Z" not in block
    assert "- Superseded: 1" not in block, "the dream report was injected"

    # -- `aft search` finds it, and by default keeps reports and the archive out.
    monkeypatch.chdir(pipeline.repo)
    assert cli.main(["search", "state in the SQLite file", "--json"]) == 0
    hits = json.loads(capsys.readouterr().out)
    assert f"projects/{SLUG}/overview.md" in [hit["path"] for hit in hits], hits

    # A phrase only the report holds is not memory, and shows up only once reports are asked for.
    assert cli.main(["search", "Revert", "--json"]) == 0
    by_default = json.loads(capsys.readouterr().out)
    assert pipeline.report_text().count("## Revert") == 1
    assert f"reports/2026/10/{DREAM_DAY}.md" not in [hit["path"] for hit in by_default], by_default
    assert cli.main(["search", "Revert", "--all", "--reports", "--json"]) == 0
    with_reports = json.loads(capsys.readouterr().out)
    assert f"reports/2026/10/{DREAM_DAY}.md" in [hit["path"] for hit in with_reports], with_reports

    # A superseded line is memory's history rather than memory, so the default query does not hand
    # it back - and the opened archive does, from the page that still carries it, which is where a
    # struck line sits until `dream.maintain` moves it at 90 days (DESIGN 10, BUILD-PLAN F20). The
    # walk used to expect it from neither, only from the digest that proposed it; the ruling that
    # opened F20 is the reason that expectation moved, and the test below holds the same promise.
    assert indexmod.ARCHIVE not in {hit["collection"] for hit in by_default}
    phrase = f'"{CLAUDE_STATEMENT.removesuffix(".")}"'
    assert cli.main(["search", phrase, "--all", "--json"]) == 0
    as_memory = json.loads(capsys.readouterr().out)
    assert f"projects/{SLUG}/overview.md" not in [hit["path"] for hit in as_memory], as_memory
    assert cli.main(["search", phrase, "--all", "--archive", "--json"]) == 0
    history = json.loads(capsys.readouterr().out)
    assert [
        hit["collection"] for hit in history if hit["path"] == f"projects/{SLUG}/overview.md"
    ] == [indexmod.ARCHIVE], history
    assert "digests" in {hit["collection"] for hit in history}, history

    # -- `aft forget session` on the Claude session, and nothing of it left afterwards.
    # Both phrases are in the index before the forgetting - the quote as the page's source, the
    # sentence as archived evidence - so their absence below says something rather than nothing.
    assert CLAUDE_DECISION_QUOTE in pipeline.index_text()
    assert CLAUDE_ONLY in pipeline.index_text()
    assert CLAUDE_DECISION_QUOTE in pipeline.report_text(), "the report never quoted the session"
    words = pipeline.words_of(CLAUDE_SESSION)
    assert any(CLAUDE_ONLY in text for text in words)
    assert cli.main(["forget", "session", CLAUDE_SESSION]) == 0
    # The sweep works over files it finds idle, so age both transcripts past the idle mark.
    # The forgotten session is still sitting in the directory the sweep reads: a capture that
    # had forgotten the forgetting would file it again here.
    age(pipeline.transcripts[0], WORKED_AGAIN_AT - timedelta(hours=1))
    age(pipeline.transcripts[1], WORKED_AGAIN_AT - timedelta(hours=1))
    again = work.run(cfg, now=WORKED_AGAIN_AT)
    # Exactly one session was swept, and it was the one nobody forgot: the sweep is awake
    # and reading this directory, and the forgotten transcript is declined on the record
    # rather than because the sweep never looked at it.
    assert again.swept == 1, again.line()
    assert again.empty == 1, again.line()
    archive = pipeline.evidence_text()
    for text in words:
        assert text not in archive, f"{text[:40]}... is still in the evidence archive"
    assert CLAUDE_SECRET not in archive

    assert evidence.entries(cfg, CLAUDE_SESSION) == []
    tree = pipeline.vault_text()
    for text in words:
        assert text not in tree, f"{text[:40]}... is still in the vault"
    assert CLAUDE_DECISION_QUOTE not in tree, "a quote of the session survived in a page source"
    assert CLAUDE_DECISION_QUOTE not in pipeline.report_text()
    assert CLAUDE_DECISION_QUOTE not in pipeline.index_text()
    assert CLAUDE_ONLY not in pipeline.index_text()

    # The line that rested only on it is withdrawn: struck, out of memory, and not searchable
    # even once the archive and the reports are opened up.
    _, memory_after = pipeline.memory()
    struck = [fact for fact in memory_after.facts if CLAUDE_STATEMENT in fact.statement]
    assert [fact.state for fact in struck] == [page.WITHDRAWN], memory_after.facts
    assert cli.main(["inject", str(pipeline.repo)]) == 0
    assert CLAUDE_STATEMENT not in capsys.readouterr().out

    monkeypatch.chdir(pipeline.repo)
    # A phrase, not a fistful of words: search terms answer on their own, so "runs" and "the"
    # would come back from every page in the vault and say nothing about this session.
    assert (
        cli.main(["search", f'"{CLAUDE_ONLY}"', "--all", "--archive", "--reports", "--json"]) == 0
    )
    assert json.loads(capsys.readouterr().out) == [], "the forgotten session is still findable"

    # And it does not come back: the queue remembers the forgetting, so neither a later sweep
    # nor the agent's own hook files the session again.
    assert not list((cfg.vault / "digests").rglob(f"*{CLAUDE_SESSION}*"))
    assert (
        hook(
            "claude",
            "SessionEnd",
            {
                "session_id": CLAUDE_SESSION,
                "transcript_path": str(pipeline.transcripts[0]),
                "cwd": str(pipeline.repo),
                "reason": "other",
            },
            monkeypatch,
        )
        == 0
    )
    assert work.run(cfg, now=WORKED_AGAIN_AT + timedelta(minutes=5)).digested == 0
    assert not list((cfg.vault / "digests").rglob(f"*{CLAUDE_SESSION}*"))
    assert CLAUDE_ONLY not in pipeline.vault_text()


def test_a_superseded_line_answers_under_the_archive_flag(pipeline: Pipeline) -> None:
    """The line the dream superseded today is still memory's history, and findable as such.

    It is on its page rather than under ``archive/``, because ``dream.maintain`` does not move a
    line until it has rested 90 days (DESIGN 9), and BUILD-PLAN F20 rules that the archive
    collection reaches it where it stands: found with ``--archive``, never without it."""
    cfg = pipeline.cfg
    pipeline.dream()

    # The words only that line ever said, quoted so the query asks for the phrase and not for
    # any of its common words on their own.
    phrase = f'"{CLAUDE_STATEMENT.removesuffix(".")}"'
    without = search.search(cfg, phrase, project=SLUG)
    assert f"projects/{SLUG}/overview.md" not in {hit.path for hit in without}, [
        (hit.path, hit.collection) for hit in without
    ]

    hits = search.search(cfg, phrase, project=SLUG, archive=True)
    paths = {hit.path for hit in hits}
    assert f"projects/{SLUG}/overview.md" in paths or any(
        path.startswith("archive/") for path in paths
    ), f"the superseded line answered only from: {sorted(paths)}"
    assert any(hit.collection == indexmod.ARCHIVE for hit in hits), [
        (hit.path, hit.collection) for hit in hits
    ]
