"""The newest session reconciles a project's short-term memory (BUILD-PLAN F47).

When a dream run applies a project's digests, the newest primary session applied is read
against the numbered lines on ``recent.md``: a line that session showed done, false or
replaced is withdrawn on a quote the script verifies against the archived entry it names,
under the same authority table and R4 a supersede answers to, and everything the
session's words do not back changes nothing and is reported.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from aft import config as configmod
from aft import digest, evidence, lint, vault
from aft.dream import apply, reconcile, schedule
from aft.dream import report as reportmod
from aft.readers import ASSISTANT, USER, Entry
from fakes.model_server import Reply
from test_apply import NOW, TODAY, fake, reply, write_digest
from test_short_term import (  # noqa: F401  (cfg is a fixture)
    RECENT,
    SLUG,
    cfg,
    paused_line,
    project_id,
    recent_text,
    seed_recent,
    session_digest,
    source,
)

__all__ = ["cfg", "fake"]  # the fixtures, imported so pytest finds them here

FILLER = "Worked through the queue backlog one item at a time, checking each pending row. "

LINES_REPLY = Reply(content={"lines": []})


def answer(line: int, quote: str, entry: str = "#1") -> Reply:
    return Reply(content={"lines": [{"line": line, "quote": quote, "entry": entry}]})


def two_lines(config) -> None:
    """Two active lines on recent.md, each sourced from session s10 with its quote kept."""
    seed_recent(
        config,
        [
            "- The hook rewrite is paused. [observed] 2026-10-01 [^r0]",
            "- The flaky test is quarantined. [observed] 2026-10-02 [^r1]",
        ],
        [
            source("r0", "s10", "The hook rewrite is paused."),
            source("r1", "s10", "The flaky test is quarantined."),
        ],
    )


def write_worker_digest(config) -> None:
    write_digest(
        config,
        SLUG,
        "2026-10-06-c-w6",
        facts=[
            {
                "statement": "Lunch was late.",
                "kind": "state",
                "source": "observed",
                "date": TODAY,
                "ref": "s90",
                "scope": "project",
                "term": "short",
            }
        ],
        sources=[source("s90", "w6", "Lunch was late.")],
        role="worker",
        at="2026-10-06T08:00:00Z",
    )


def write_no_evidence_digest(config) -> None:
    """The newest digest waiting, whose session's entries were never archived."""
    write_digest(
        config,
        SLUG,
        "2026-10-10-c-s10",
        facts=[],
        sources=[],
        at="2026-10-10T08:00:00Z",
        store_evidence=False,
    )


def write_import_digest(config, filename: str) -> Path:
    """A library or memory-import digest waiting for the run, named as its writer names
    one: the imported material's name sits where a session id would (propose writes
    ``<date>-library-<collection>``, a memory import ``<date>-import-<name>-<hash>``)."""
    rel = f"digests/{SLUG}/2026/10/{filename}.md"
    fm = {
        "type": "Session Digest",
        "title": f"Digest {filename}",
        "description": "Proposed facts from imported material.",
        "generated": {"by": "aft", "at": "2026-10-10T08:00:00Z"},
        "sources": [],
        "aft": {"project": project_id(config), "facts": []},
    }
    path = config.vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n\n")
    vault.commit(config.vault, [rel], f"seed import digest {filename}")
    return path


def seed_preference(cfg) -> Path:
    """The [stated] preference line on recent.md, under Preferences, with its source."""
    path = cfg.vault / RECENT
    fm = {
        "type": "Project",
        "title": "Widget recent",
        "description": "Short-term memory.",
        "sources": [source("r0", "s10", "Keep the queue on SQLite.")],
        "aft": {"project": project_id(cfg)},
    }
    body = "## Preferences\n\n- Keep the queue on SQLite. [stated] 2026-10-01 [^r0]\n"
    path.write_text(f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n\n{body}")
    vault.commit(cfg.vault, [RECENT], "seed preference")
    return path


def long_entries(config, *texts: str) -> None:
    """Archive the session's entries, each one long enough to fill its own call's cap."""
    entries = [
        Entry(id=f"e5{n}", time=NOW, role=USER, text=text, is_error=False)
        for n, text in enumerate(texts)
    ]
    evidence.store(
        config, agent="c", session_id="s10", project_id=project_id(config), entries=entries
    )


# --------------------------------------------------------------------- the withdrawal


def test_a_done_line_is_withdrawn_on_its_verified_quote(cfg, fake):
    session_digest(cfg, 10, waiting=True)
    paused_line(cfg, "s10")
    fake.script = [answer(1, "The hook rewrite is paused.", "#1")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert len(fake.requests) == 1, "one call for the one-part session"
    assert [(w.statement, w.quote) for w in rep.withdrawn] == [
        ("The hook rewrite is paused.", "The hook rewrite is paused.")
    ]
    assert rep.refused == []
    body = (cfg.vault / RECENT).read_text()
    assert (
        "~~The hook rewrite is paused.~~ [observed] 2026-10-01 [^r0] withdrawn 2026-10-05" in body
    )
    assert lint.lint_vault(cfg.vault) == []
    section = reportmod.format_run_section(rep, cfg.vault, NOW)
    assert 'Withdrawn: "The hook rewrite is paused."' in section
    assert 'Quote: "The hook rewrite is paused."' in section


def test_a_wrong_quote_changes_nothing_and_is_reported(cfg, fake):
    session_digest(cfg, 10, waiting=True)
    paused_line(cfg, "s10")
    fake.script = [answer(1, "The rewrite resumed and is finished now.", "#1")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert len(fake.requests) == 1
    assert rep.withdrawn == []
    assert [(r.rule, r.statement) for r in rep.refused] == [("R1", "The hook rewrite is paused.")]
    assert "The hook rewrite is paused. [observed] 2026-10-01 [^r0]" in recent_text(cfg)
    section = reportmod.format_run_section(rep, cfg.vault, NOW)
    assert (
        'Not withdrawn: "The hook rewrite is paused."'
        " (rule: R1 - the quote is not in entry e70)" in section
    )


def test_a_long_term_line_number_changes_nothing(cfg, fake):
    """A number outside the short-term lines the run numbered, a long-term line's place on
    another page, is refused and changes nothing."""
    session_digest(cfg, 10, waiting=True)
    paused_line(cfg, "s10")
    fake.script = [answer(3, "The hook rewrite is paused.", "#1")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert rep.withdrawn == []
    assert rep.refused[0].rule == "reconcile"
    assert "line 3 is not a line this run numbered" in rep.refused[0].reason
    assert "The hook rewrite is paused. [observed] 2026-10-01 [^r0]" in recent_text(cfg)
    section = reportmod.format_run_section(rep, cfg.vault, NOW)
    assert "(rule: reconcile - line 3 is not a line this run numbered)" in section


def test_a_noted_line_is_never_numbered_so_never_withdrawn(cfg, fake):
    """A line with an open conflict or question waits on the user (design 6): a note's
    state is the note's, so the line is never among the numbered lines. The model is
    never shown it - the lines block names the active line only, and the note's text
    reaches no call - an answer naming the number it would have had is refused, and the
    line beside it is withdrawn as usual."""
    session_digest(cfg, 10, waiting=True)
    seed_recent(
        cfg,
        [
            "- The flaky test is quarantined. [observed] 2026-10-02 [^r1]",
            "- The hook rewrite is paused. [observed] 2026-10-01 [^r0]",
            "",
            "> [!conflict] 2026-10-01",
            "> Another session says it resumed.",
            "> My answer: it stays paused.",
        ],
        [
            source("r0", "s10", "The hook rewrite is paused."),
            source("r1", "s10", "The flaky test is quarantined."),
        ],
    )
    entries = [
        Entry(id=eid, time=NOW, role=USER, text=words, is_error=False)
        for eid, words in (
            ("e70", "The hook rewrite is paused."),
            ("e71", "The flaky test is quarantined."),
        )
    ]
    evidence.store(cfg, agent="c", session_id="s10", project_id=None, entries=entries)
    # The part labels the session's entries in order, so the second entry is #2 here, and
    # line 2 is the number the noted line would have had if it were numbered.
    fake.script = [
        Reply(
            content={
                "lines": [
                    {"line": 1, "quote": "The flaky test is quarantined.", "entry": "#2"},
                    {"line": 2, "quote": "The hook rewrite is paused.", "entry": "#1"},
                ]
            }
        )
    ]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert rep.error is None, rep.error
    assert len(fake.requests) == 1
    body = fake.requests[0]["messages"][1]["content"]
    lines_block = body.split("\n\nSession entries:")[0]
    assert "The hook rewrite is paused." not in lines_block, "the noted line was numbered"
    assert "Another session says it resumed." not in body, "the note reached the model"
    assert "My answer: it stays paused." not in body, "the note reached the model"
    assert rep.refused == [
        reconcile.RefusedWithdrawal(
            statement="line 2",
            rule="reconcile",
            reason="line 2 is not a line this run numbered",
        )
    ]
    assert [w.statement for w in rep.withdrawn] == ["The flaky test is quarantined."]
    text = (cfg.vault / RECENT).read_text()
    assert "The hook rewrite is paused. [observed] 2026-10-01 [^r0]" in text
    assert "My answer: it stays paused." in text
    assert "The flaky test is quarantined. [observed] 2026-10-02 [^r1]" not in text


def test_a_stated_preference_is_withdrawn_only_on_your_own_words(cfg, fake):
    """The authority table (DESIGN 9): a [stated] preference is replaced by your newer
    [stated], never by what a session observed - so the newest session's words about the
    preference withdraw nothing, and the line stands."""
    session_digest(cfg, 10, waiting=True)
    seed_preference(cfg)
    entries = [
        Entry(id="e70", time=NOW, role=USER, text="Keep the queue on SQLite.", is_error=False),
        Entry(
            id="e71",
            time=NOW,
            role=ASSISTANT,
            text="The queue moved to Postgres today.",
            is_error=False,
        ),
    ]
    evidence.store(cfg, agent="c", session_id="s10", project_id=None, entries=entries)
    fake.script = [answer(1, "The queue moved to Postgres today.", "#2")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert len(fake.requests) == 1
    assert rep.withdrawn == []
    assert [(r.rule, r.statement) for r in rep.refused] == [("R4", "Keep the queue on SQLite.")]
    assert "a preference line is withdrawn only on your own words" in rep.refused[0].reason
    assert "Keep the queue on SQLite. [stated] 2026-10-01 [^r0]" in recent_text(cfg)
    section = reportmod.format_run_section(rep, cfg.vault, NOW)
    assert (
        'Not withdrawn: "Keep the queue on SQLite."'
        " (rule: R4 - a preference line is withdrawn only on your own words)" in section
    )


def test_an_agent_claim_without_tool_backing_cannot_withdraw_a_stated_line(cfg, fake):
    """M2: an agent claim (assistant text without tool backing) is inferred and cannot
    withdraw a user stated line under R4."""
    session_digest(cfg, 10, waiting=True)
    seed_recent(
        cfg,
        ["- The build command is uv run pytest. [stated] 2026-10-01 [^r0]"],
        [source("r0", "s10", "The build command is uv run pytest.")],
    )
    entries = [
        Entry(
            id="e70",
            time=NOW,
            role=ASSISTANT,
            text="The build command has changed to cargo test.",
            is_error=False,
        ),
    ]
    evidence.store(cfg, agent="c", session_id="s10", project_id=None, entries=entries)
    fake.script = [answer(1, "The build command has changed to cargo test.", "#1")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert len(fake.requests) == 1
    assert rep.withdrawn == []
    assert [(r.rule, r.statement) for r in rep.refused] == [
        ("R4", "The build command is uv run pytest.")
    ]
    assert "only on your own words or [observed] evidence" in rep.refused[0].reason
    assert "The build command is uv run pytest. [stated] 2026-10-01 [^r0]" in recent_text(cfg)


def test_your_own_newer_words_withdraw_your_preference(cfg, fake):
    """The authority table's positive row: your own later words replace your earlier ones,
    so a withdrawal on them happens."""
    session_digest(cfg, 10, waiting=True)
    seed_preference(cfg)
    entries = [
        Entry(id="e70", time=NOW, role=USER, text="Keep the queue on SQLite.", is_error=False),
        Entry(
            id="e71",
            time=NOW,
            role=USER,
            text="I changed my mind: the queue moves to Postgres.",
            is_error=False,
        ),
    ]
    evidence.store(cfg, agent="c", session_id="s10", project_id=None, entries=entries)
    fake.script = [answer(1, "I changed my mind: the queue moves to Postgres.", "#2")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert rep.refused == []
    assert [(w.statement, w.quote) for w in rep.withdrawn] == [
        ("Keep the queue on SQLite.", "I changed my mind: the queue moves to Postgres.")
    ]
    assert (
        "~~Keep the queue on SQLite.~~ [stated] 2026-10-01 [^r0] withdrawn 2026-10-05"
        in (cfg.vault / RECENT).read_text()
    )


def test_words_older_than_the_line_withdraw_nothing(cfg, fake):
    """R4's date half: the quoting entry must be newer than the line's own date, so a line
    added today by hand is not withdrawn on words from days ago."""
    session_digest(cfg, 10, waiting=True)
    seed_recent(
        cfg,
        ["- The hook rewrite is paused. [observed] 2026-10-05 [^r0]"],
        [source("r0", "s10", "The hook rewrite is paused.")],
    )
    entry = Entry(
        id="e70",
        time=NOW - timedelta(days=3),
        role=USER,
        text="The hook rewrite is paused.",
        is_error=False,
    )
    evidence.store(cfg, agent="c", session_id="s10", project_id=None, entries=[entry])
    fake.script = [answer(1, "The hook rewrite is paused.", "#1")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert len(fake.requests) == 1
    assert rep.withdrawn == []
    assert [(r.rule, r.statement) for r in rep.refused] == [("R4", "The hook rewrite is paused.")]
    assert (
        "the quote is from 2026-10-02, not newer than the line's 2026-10-05"
        in rep.refused[0].reason
    )
    assert "The hook rewrite is paused. [observed] 2026-10-05 [^r0]" in recent_text(cfg)


def test_a_withdrawn_line_is_not_also_expired_in_the_same_run(cfg, fake):
    """The reconcile's withdrawal is the line's leaving for this run: the day backstop
    does not strike it again in the same run, and the report lists it once."""
    session_digest(cfg, 10, waiting=True)
    paused_line(cfg, "s1", at=NOW - timedelta(days=10))
    evidence.store(
        cfg,
        agent="c",
        session_id="s10",
        project_id=None,
        entries=[
            Entry(
                id="e80",
                time=NOW,
                role=USER,
                text="The hook rewrite is done and merged.",
                is_error=False,
            )
        ],
    )
    fake.script = [answer(1, "The hook rewrite is done and merged.", "#1")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert [w.statement for w in rep.withdrawn] == ["The hook rewrite is paused."]
    assert rep.expired == [], "the backstop expired the line the reconcile just withdrew"
    assert rep.rolled_off == []
    assert "withdrawn 2026-10-05" in (cfg.vault / RECENT).read_text()
    section = reportmod.format_run_section(rep, cfg.vault, NOW)
    assert 'Withdrawn: "The hook rewrite is paused."' in section
    assert 'Expired: "The hook rewrite is paused."' not in section


# --------------------------------------------------------------------- when no call is made


def test_a_worker_session_reconciles_nothing(cfg, fake):
    """A worker's digest is no session of the project's own work (DESIGN 8): even as the
    newest digest waiting, its entries never reconcile short-term memory."""
    session_digest(cfg, 1)
    paused_line(cfg, "s1")
    write_worker_digest(cfg)
    fake.script = [reply("skip")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert len(fake.requests) == 1, "the decide call for the worker's fact, and no more"
    assert rep.withdrawn == []
    assert "The hook rewrite is paused. [observed] 2026-10-01 [^r0]" in recent_text(cfg)


def test_a_session_not_in_the_archive_reconciles_nothing(cfg, fake):
    """The archive no longer holds the session's entries - forgotten, purged, never
    captured - so no part of it can be put to the lines (DESIGN 14)."""
    two_lines(cfg)
    write_no_evidence_digest(cfg)
    fake.script = []
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert fake.requests == [], "the fact-less digest decides nothing, and no part is called"
    assert rep.withdrawn == []
    assert "The flaky test is quarantined. [observed] 2026-10-02 [^r1]" in recent_text(cfg)


def test_no_recent_lines_make_no_call(cfg, fake):
    """A project without short-term memory puts nothing to the model, however full its
    digests are."""
    session_digest(cfg, 1, facts=[], sources=[], waiting=True)
    fake.script = []
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert fake.requests == []
    assert rep.withdrawn == []


@pytest.mark.parametrize(
    "filename", ["2026-10-10-library-s10", "2026-10-10-import-s10"], ids=["library", "import"]
)
def test_an_imported_digest_reconciles_nothing(cfg, fake, filename):
    """A library or memory-import digest is no session of the project's own work (DESIGN
    8, DESIGN 11): as the only digest applied it reconciles nothing - and when the
    imported material is named like an archived session id, the session's entries are not
    read under that name."""
    paused_line(cfg, "s10")
    write_import_digest(cfg, filename)
    fake.script = []
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert fake.requests == [], "the imported material is no session to read"
    assert rep.withdrawn == []
    assert "The hook rewrite is paused. [observed] 2026-10-01 [^r0]" in recent_text(cfg)


def test_a_recent_page_with_no_active_line_makes_no_call(cfg, fake):
    """A recent.md whose every line already stands struck has nothing for the session to
    settle: no part of it is put to the model and the page is left as it is."""
    session_digest(cfg, 10, waiting=True)
    seed_recent(
        cfg,
        ["- ~~The hook rewrite is paused.~~ [observed] 2026-10-01 [^r0] withdrawn 2026-10-04"],
        [source("r0", "s10", "The hook rewrite is paused.")],
    )
    fake.script = []
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert fake.requests == []
    assert rep.withdrawn == []
    assert rep.refused == []
    assert "withdrawn 2026-10-04" in (cfg.vault / RECENT).read_text()


def spend(config, tokens: int) -> None:
    """Record ``tokens`` of model use earlier today, as aft.model logs a call."""
    line = {"at": NOW.replace(hour=1).isoformat(), "prompt_tokens": tokens, "completion_tokens": 0}
    with (config.home / "usage.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def budget_config(cfg, home) -> configmod.Config:
    (home / "config.toml").write_text(
        (home / "config.toml").read_text() + "[work]\ndaily_token_budget = 250\n"
    )
    return configmod.load()


def test_the_spent_budget_skips_the_reconcile(cfg, fake, home):
    """The reconcile counts against the day's token budget like every model call; when the
    day's budget is spent, it is skipped without a call or a report line."""
    digest = session_digest(cfg, 10, waiting=True)
    paused_line(cfg, "s10")
    config = budget_config(cfg, home)
    spend(config, 250)
    rep = apply.ProjectRunReport(project=SLUG)
    reconcile.run(config, config.vault, SLUG, [digest], TODAY, rep, lambda p: None, now=NOW)
    assert fake.requests == []
    assert rep.withdrawn == []
    assert rep.refused == []
    assert "The hook rewrite is paused. [observed] 2026-10-01 [^r0]" in recent_text(config)


def test_a_budget_with_room_lets_the_reconcile_run(cfg, fake, home):
    digest = session_digest(cfg, 10, waiting=True)
    paused_line(cfg, "s10")
    config = budget_config(cfg, home)
    spend(config, 249)
    fake.script = [answer(1, "The hook rewrite is paused.", "#1")]
    rep = apply.ProjectRunReport(project=SLUG)
    reconcile.run(config, config.vault, SLUG, [digest], TODAY, rep, lambda p: None, now=NOW)
    assert len(fake.requests) == 1
    assert [w.statement for w in rep.withdrawn] == ["The hook rewrite is paused."]


# --------------------------------------------------------------------- the parts


def test_a_part_per_cap_two_calls_two_withdrawals(cfg, fake):
    """The session's entries are split under the digest's chunk cap, one call per part, and
    every part gets the same numbered lines: two long entries, two calls, two withdrawals."""
    two_lines(cfg)
    long_entries(
        cfg,
        FILLER * 300 + "The hook rewrite is paused.",
        FILLER * 300 + "The flaky test is quarantined.",
    )
    session_digest(cfg, 10, waiting=True)
    fake.script = [
        answer(1, "The hook rewrite is paused.", "e50"),
        answer(2, "The flaky test is quarantined.", "e51"),
    ]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert len(fake.requests) == 2, "one call per part under the cap"
    assert [w.statement for w in rep.withdrawn] == [
        "The hook rewrite is paused.",
        "The flaky test is quarantined.",
    ]
    body = (cfg.vault / RECENT).read_text()
    assert body.count("withdrawn 2026-10-05") == 2


def test_the_budget_stops_the_parts_left(cfg, fake, home):
    """The budget is checked before each part, not once for the whole step: a session of
    many parts stops at the day's last affordable call, and the parts after it wait for
    the next day instead of spending into a budget already used up."""
    two_lines(cfg)
    long_entries(
        cfg,
        FILLER * 300 + "The hook rewrite is paused.",
        FILLER * 300 + "The flaky test is quarantined.",
    )
    config = budget_config(cfg, home)
    # The ledger stamps every call with the real clock, so the run's clock here is the
    # real day's start: the pre-spend and the first call's own tokens both count within
    # it, and the check before the second part sees the budget already used up.
    now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    spend(config, 100)
    fake.script = [
        answer(1, "The hook rewrite is paused.", "e50"),
        answer(2, "The flaky test is quarantined.", "e51"),
    ]
    rep = apply.ProjectRunReport(project=SLUG)
    digest = session_digest(cfg, 10, waiting=True)
    reconcile.run(config, config.vault, SLUG, [digest], TODAY, rep, lambda p: None, now=now)
    assert len(fake.requests) == 1, "the day's last affordable call, not every part"
    assert [w.statement for w in rep.withdrawn] == ["The hook rewrite is paused."]
    assert (
        "The flaky test is quarantined. [observed] 2026-10-02 [^r1]"
        in (config.vault / RECENT).read_text()
    )


def test_the_lines_hold_their_numbers_across_parts(cfg, fake):
    """The numbered lines are the same for every part, so each part's answer names the
    lines by one numbering; a part shows a line and the script withdraws that line."""
    two_lines(cfg)
    long_entries(
        cfg,
        FILLER * 300 + "Nothing came up about the hooks.",
        FILLER * 300 + "The flaky test is quarantined.",
    )
    session_digest(cfg, 10, waiting=True)
    # The second part's answer names line 2 by the numbering every part was given.
    fake.script = [LINES_REPLY, answer(2, "The flaky test is quarantined.", "#1")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert len(fake.requests) == 2
    assert [w.statement for w in rep.withdrawn] == ["The flaky test is quarantined."]


# --------------------------------------------------------------------- the answer cap


def test_the_answer_cap_holds_the_schema_s_worst_answer(cfg, fake):
    """The output cap one call is given holds the biggest answer the schema allows: an
    answer cut off at its cap is lost whole, the call fails its schema, and the project
    rolls back to hit the same wall on every later run."""
    session_digest(cfg, 10, waiting=True)
    paused_line(cfg, "s10")
    fake.script = [answer(1, "The hook rewrite is paused.", "#1")]
    schedule.run(cfg, now=NOW)
    lines_schema = reconcile.schema()["properties"]["lines"]
    worst = {
        "lines": [
            {
                "line": lines_schema["maxItems"],
                "quote": "x" * lines_schema["items"]["properties"]["quote"]["maxLength"],
                "entry": "#99",
            }
        ]
        * lines_schema["maxItems"]
    }
    sent = fake.requests[0]["max_tokens"]
    assert sent >= digest.tokens(json.dumps(worst)), (
        f"the cap {sent} cannot hold the schema's worst answer"
    )


def test_a_full_answer_is_applied(cfg, fake):
    """The schema's worst answer - one item per numbered line, maxItems of them - applies
    whole: every line it settles is withdrawn once and none is refused."""
    session_digest(cfg, 10, waiting=True)
    seed_recent(
        cfg,
        [f"- Line {n} is on hold. [observed] 2026-10-01 [^r{n - 1}]" for n in range(1, 21)],
        [
            source(f"r{n - 1}", "s10", f"Line {n} is on hold.", entry=f"e{n:02d}")
            for n in range(1, 21)
        ],
    )
    evidence.store(
        cfg,
        agent="c",
        session_id="s10",
        project_id=None,
        entries=[
            Entry(
                id=f"e{n:02d}",
                time=NOW,
                role=USER,
                text=f"Line {n} is on hold. Line {n} is done now.",
                is_error=False,
            )
            for n in range(1, 21)
        ],
    )
    fake.script = [
        Reply(
            content={
                "lines": [
                    {"line": n, "quote": f"Line {n} is done now.", "entry": f"#{n}"}
                    for n in range(1, 21)
                ]
            }
        )
    ]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert len(fake.requests) == 1, "twenty short entries fit one part"
    assert [w.statement for w in rep.withdrawn] == [f"Line {n} is on hold." for n in range(1, 21)]
    assert rep.refused == []
    assert (cfg.vault / RECENT).read_text().count("withdrawn 2026-10-05") == 20
    assert lint.lint_vault(cfg.vault) == []


# --------------------------------------------------------------------- what session is read
def test_the_entries_are_the_agent_s_own_session(cfg, fake):
    """The archive is keyed by the session and entry ids alone, so two agents' sessions
    can share an id: the reconcile reads the newest session's entries by agent and id,
    another agent's rows under the same id are no evidence here, and their words reach
    no call."""
    session_digest(cfg, 10, waiting=True)
    paused_line(cfg, "s10")
    other = Entry(
        id="e90",
        time=NOW,
        role=ASSISTANT,
        text="The rewrite resumed and is finished now.",
        is_error=False,
    )
    evidence.store(cfg, agent="pi", session_id="s10", project_id=None, entries=[other])
    fake.script = [answer(1, "The rewrite resumed and is finished now.", "e90")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert len(fake.requests) == 1
    assert "resumed and is finished" not in fake.requests[0]["messages"][1]["content"], (
        "another agent's session was put to the lines"
    )
    assert rep.withdrawn == []
    assert [(r.rule, r.statement) for r in rep.refused] == [
        ("reconcile", "The hook rewrite is paused.")
    ]
    assert "The hook rewrite is paused. [observed] 2026-10-01 [^r0]" in recent_text(cfg)


def test_a_library_digest_last_leaves_the_session_to_reconcile(cfg, fake):
    """A library digest applied after the session's is no session of the project (DESIGN
    11): the newest primary session applied is still the session, so its entries are read
    against the lines and its quote withdraws."""
    session_digest(cfg, 10, waiting=True)
    paused_line(cfg, "s10")
    write_import_digest(cfg, "2026-10-10-library-manual")
    fake.script = [answer(1, "The hook rewrite is paused.", "#1")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert len(fake.requests) == 1, "the library digest is not the session to read"
    assert [w.statement for w in rep.withdrawn] == ["The hook rewrite is paused."]


def test_newest_primary_takes_the_newest_applied_primary(cfg):
    session_digest(cfg, 10, waiting=True)
    write_worker_digest(cfg)
    digests = sorted((cfg.vault / f"digests/{SLUG}").rglob("*.md"))
    assert reconcile.newest_primary(cfg.vault, digests) == ("c", "s10")


def test_newest_primary_folds_a_later_part_into_its_session(cfg):
    session_digest(cfg, 10, waiting=True)
    write_digest(
        cfg,
        SLUG,
        "2026-10-10-c-s10-2",
        facts=[],
        sources=[],
        at="2026-10-10T14:00:00Z",
    )
    digests = sorted((cfg.vault / f"digests/{SLUG}").rglob("*.md"))
    assert reconcile.newest_primary(cfg.vault, digests) == ("c", "s10")


def test_newest_primary_with_nothing_applied_is_none():
    assert reconcile.newest_primary(Path("/nowhere"), []) is None


# --------------------------------------------------------------------- the shipped prompt


def test_the_prompt_and_schema_ship():
    text = reconcile.prompt()
    assert "short-term" in text
    assert "between the user and an AI assistant, often a coding agent" in text
    assert "Ted" not in text, "the shipped prompt names no one"
    schema = reconcile.schema()
    assert schema["required"] == ["lines"]
    item = schema["properties"]["lines"]["items"]
    assert item["required"] == ["line", "quote", "entry"]
    assert item["properties"]["quote"]["maxLength"] == 300


def _same_day(cfg, fake_endpoint, line_at: datetime, said_at: datetime) -> apply.ProjectRunReport:
    """A recent line from today's earlier session s9, and session s10's words about it."""
    session_digest(cfg, 10, waiting=True)
    seed_recent(
        cfg,
        [f"- The hook rewrite is paused. [observed] {TODAY} [^r0]"],
        [source("r0", "s9", "The hook rewrite is paused.", entry="e9")],
    )
    paused = Entry(
        id="e9", time=line_at, role=ASSISTANT, text="The hook rewrite is paused.", is_error=False
    )
    evidence.store(cfg, agent="c", session_id="s9", project_id=None, entries=[paused])
    resumed = Entry(
        id="e70", time=said_at, role=USER, text="The hook rewrite is resumed.", is_error=False
    )
    evidence.store(cfg, agent="c", session_id="s10", project_id=None, entries=[resumed])
    fake_endpoint.script = [answer(1, "The hook rewrite is resumed.", "#1")]
    return schedule.run(cfg, now=NOW).reports[SLUG]


def test_a_later_entry_the_same_day_withdraws_a_recent_line(cfg, fake):
    """F58: this morning's line is withdrawn by this afternoon's words, though R4's day
    is the same."""
    rep = _same_day(cfg, fake, NOW - timedelta(hours=6), NOW - timedelta(hours=1))
    assert [w.statement for w in rep.withdrawn] == ["The hook rewrite is paused."]
    assert rep.refused == []


def test_an_earlier_entry_the_same_day_withdraws_nothing(cfg, fake):
    rep = _same_day(cfg, fake, NOW - timedelta(hours=1), NOW - timedelta(hours=6))
    assert rep.withdrawn == []
    assert [r.rule for r in rep.refused] == ["R4"]


def test_a_source_the_archive_does_not_hold_leaves_the_day_to_decide(cfg, fake):
    """With no source time to compare, the same day is not newer (R4's day alone)."""
    session_digest(cfg, 10, waiting=True)
    seed_recent(
        cfg,
        [f"- The hook rewrite is paused. [observed] {TODAY} [^r0]"],
        [source("r0", "s9", "The hook rewrite is paused.", entry="e9")],
    )
    resumed = Entry(
        id="e70", time=NOW, role=USER, text="The hook rewrite is resumed.", is_error=False
    )
    evidence.store(cfg, agent="c", session_id="s10", project_id=None, entries=[resumed])
    fake.script = [answer(1, "The hook rewrite is resumed.", "#1")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert rep.withdrawn == []
    assert [r.rule for r in rep.refused] == ["R4"]
