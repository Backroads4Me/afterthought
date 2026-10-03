"""The newest session reconciles a project's short-term memory (BUILD-PLAN F47).

When a dream run applies a project's digests, the newest primary session applied is read
against the numbered lines on ``recent.md``: a line that session showed done, false or
replaced is withdrawn on a quote the script verifies against the archived entry it names,
and everything the session's words do not back changes nothing and is reported.
"""

from __future__ import annotations

import json
from pathlib import Path

from aft import config as configmod
from aft import evidence, lint
from aft.dream import apply, reconcile, schedule
from aft.dream import report as reportmod
from aft.readers import USER, Entry
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
    state is the note's, so the line is never among the numbered lines, and the session's
    words about it withdraw nothing - the line beside it is withdrawn as usual."""
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
    # The part labels the session's entries in order, so the second entry is #2 here.
    fake.script = [answer(1, "The flaky test is quarantined.", "#2")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert rep.error is None, rep.error
    assert len(fake.requests) == 1
    assert rep.refused == []
    assert [w.statement for w in rep.withdrawn] == ["The flaky test is quarantined."]
    text = (cfg.vault / RECENT).read_text()
    assert "The hook rewrite is paused. [observed] 2026-10-01 [^r0]" in text
    assert "My answer: it stays paused." in text
    assert "The flaky test is quarantined. [observed] 2026-10-02 [^r1]" not in text


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


# --------------------------------------------------------------------- what session is read


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
    assert "Ted" not in text, "the shipped prompt names no one"
    schema = reconcile.schema()
    assert schema["required"] == ["lines"]
    item = schema["properties"]["lines"]["items"]
    assert item["required"] == ["line", "quote", "entry"]
    assert item["properties"]["quote"]["maxLength"] == 300
