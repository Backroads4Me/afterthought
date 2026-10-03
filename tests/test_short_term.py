"""Short-term memory in the dream (BUILD-PLAN F31; DESIGN 6, DESIGN 9 R10).

A short-term fact lands on ``projects/<slug>/recent.md`` and edits long-term memory only to
confirm it. A short-term line rolls off once its newest source leaves the project's last
``dream.short_term_sessions`` sessions, counted in sessions and never in days, and becomes
long-term when its sources are that many sessions apart or a later long-term fact confirms
it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from aft import config as configmod
from aft import evidence, lint, page, pagewrite, search, vault
from aft.dream import report as reportmod
from aft.dream import rules, schedule, shortterm
from aft.readers import USER, Entry
from aft.registry import Registry
from conftest import run_git
from fakes.model_server import Reply
from test_apply import NOW, TODAY, config_with_fake, fake, reply, seed_overview, write_digest

__all__ = ["fake"]  # the fixture, imported so pytest finds it here

LINES_REPLY = Reply(content={"lines": []})


SLUG = "widget"
RECENT = f"projects/{SLUG}/recent.md"


@pytest.fixture
def cfg(initialized, home, fake) -> configmod.Config:
    config = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(config.registry_path)
    registry.add(SLUG, "Widget")
    registry.save()
    seed_overview(
        config,
        SLUG,
        body="## State\n\n- The build runs on the GPU host. [observed] 2026-09-30 [^s1]\n",
        sources=[
            {"id": "s1", "resource": "aft://session/c/s0#e1", "quote": "runs on the GPU host"}
        ],
    )
    return config


def project_id(cfg: configmod.Config) -> str:
    return Registry.load(cfg.registry_path).by_slug(SLUG).id


def session_digest(
    cfg: configmod.Config,
    n: int,
    *,
    facts: list[dict] | None = None,
    sources: list[dict] | None = None,
    waiting: bool = False,
) -> Path:
    """Session ``s<n>`` of the project, with a digest written in session order. A digest
    that is not waiting was dreamed by an earlier run."""
    return write_digest(
        cfg,
        SLUG,
        f"2026-10-0{n}-c-s{n}" if n < 10 else f"2026-10-{n}-c-s{n}",
        facts=facts or [],
        sources=sources or [],
        at=f"2026-10-{n:02d}T08:00:00Z",
        # Earlier runs consumed them; a run id ends in the time the run started.
        consumed=None if waiting else f"dream-{SLUG}-202609{n:02d}T090000Z",
    )


def short_fact(statement: str, ref: str = "s10", source: str = "observed") -> dict:
    return {
        "statement": statement,
        "kind": "state",
        "source": source,
        "date": TODAY,
        "ref": ref,
        "scope": "project",
        "term": "short",
    }


def seed_recent(cfg: configmod.Config, lines: list[str], sources: list[dict]) -> Path:
    """A recent.md written by an earlier run."""
    path = cfg.vault / RECENT
    fm = {
        "type": "Project",
        "title": "Widget recent",
        "description": "Short-term memory.",
        "sources": sources,
        "aft": {"project": project_id(cfg)},
    }
    body = "## State\n\n" + "\n".join(lines) + "\n"
    path.write_text(f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n\n{body}")
    vault.commit(cfg.vault, [RECENT], "seed recent")
    return path


def source(sid: str, session: str, quote: str, entry: str = "e1") -> dict:
    return {"id": sid, "resource": f"aft://session/c/{session}#{entry}", "quote": quote}


def recent_text(cfg: configmod.Config) -> str:
    """The page's body: its lines, without the sources that quote them."""
    return page.split_frontmatter((cfg.vault / RECENT).read_text())[1]


def overview_text(cfg: configmod.Config) -> str:
    """The overview's body: its lines, without the sources that quote them."""
    return page.split_frontmatter((cfg.vault / f"projects/{SLUG}/overview.md").read_text())[1]


# --------------------------------------------------------------------- where a fact goes


def test_a_short_term_fact_lands_on_recent_and_not_on_the_overview(cfg, fake):
    before = overview_text(cfg)
    session_digest(
        cfg,
        1,
        facts=[short_fact("The hook rewrite is paused for review.")],
        sources=[source("s10", "s1", "The hook rewrite is paused for review.")],
        waiting=True,
    )
    fake.script = [reply("add"), LINES_REPLY]
    run = schedule.run(cfg, now=NOW)
    rep = run.reports[SLUG]
    assert "- The hook rewrite is paused for review. [observed]" in recent_text(cfg)
    assert overview_text(cfg) == before
    assert lint.lint_vault(cfg.vault) == []
    section = reportmod.format_run_section(rep, cfg.vault, NOW)
    assert 'Added: "The hook rewrite is paused for review."' in section


def test_a_fact_recorded_without_a_term_is_long_term(cfg, fake):
    """A digest page with no term (library, memory import, or written before F30) is read
    as long-term, so its fact goes to the overview."""
    fact = short_fact("The queue keeps its state in SQLite.")
    del fact["term"]
    session_digest(
        cfg,
        1,
        facts=[fact],
        sources=[source("s10", "s1", "The queue keeps its state in SQLite.")],
        waiting=True,
    )
    fake.script = [reply("add")]
    schedule.run(cfg, now=NOW)
    assert "The queue keeps its state in SQLite." in overview_text(cfg)
    assert not (cfg.vault / RECENT).exists()


def test_a_short_term_fact_contradicting_a_long_term_line_is_added_to_recent(cfg, fake):
    before = overview_text(cfg)
    session_digest(
        cfg,
        1,
        facts=[short_fact("The build runs on the laptop while the GPU host is down.")],
        sources=[source("s10", "s1", "The build runs on the laptop while the GPU host is down.")],
        waiting=True,
    )
    fake.script = [reply("supersede", "L1"), LINES_REPLY]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert overview_text(cfg) == before
    assert "The build runs on the laptop while the GPU host is down." in recent_text(cfg)
    assert [a.path for a in rep.added] == [RECENT]


def test_a_short_term_fact_confirms_a_long_term_line(cfg, fake):
    session_digest(
        cfg,
        1,
        facts=[short_fact("The build still runs on the GPU host.")],
        sources=[source("s10", "s1", "The build still runs on the GPU host.")],
        waiting=True,
    )
    fake.script = [reply("confirm", "L1")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert [c.path for c in rep.confirmed] == [f"projects/{SLUG}/overview.md"]
    assert not (cfg.vault / RECENT).exists()


# --------------------------------------------------------------------- rolling off


def paused_line(cfg, *sessions: str, at: datetime = NOW) -> None:
    """The line on recent.md, one source per session it was seen in, each quote archived
    at ``at``, so a test can place the sources in the past."""
    refs = " ".join(f"[^r{n}]" for n in range(len(sessions)))
    for n, session in enumerate(sessions):
        entry = Entry(
            id=f"e7{n}", time=at, role=USER, text="The hook rewrite is paused.", is_error=False
        )
        evidence.store(cfg, agent="c", session_id=session, project_id=None, entries=[entry])
    seed_recent(
        cfg,
        [f"- The hook rewrite is paused. [observed] 2026-10-01 {refs}"],
        [
            source(f"r{n}", s, "The hook rewrite is paused.", entry=f"e7{n}")
            for n, s in enumerate(sessions)
        ],
    )


def unrelated_waiting(cfg, n: int) -> None:
    session_digest(
        cfg,
        n,
        facts=[short_fact("Lunch was late.", ref="s90")],
        sources=[source("s90", f"s{n}", "Lunch was late.")],
        waiting=True,
    )


def test_a_line_rolls_off_once_its_newest_session_leaves_the_window(cfg, fake):
    session_digest(cfg, 1)
    paused_line(cfg, "s1")
    for n in range(2, 6):
        session_digest(cfg, n)
    unrelated_waiting(cfg, 6)
    fake.script = [reply("skip"), LINES_REPLY]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    print("REP.ERROR:", rep.error)
    print("REP.ROLLED:", rep.rolled_off)
    print("REP.EXPIRED:", rep.expired)
    print("REP.WITHDRAWN:", rep.withdrawn)
    print("REP.REFUSED:", rep.refused)
    assert "The hook rewrite is paused." not in recent_text(cfg)
    assert [r.statement for r in rep.rolled_off] == ["The hook rewrite is paused."]
    assert "The hook rewrite is paused." not in (cfg.vault / RECENT).read_text()
    assert lint.lint_vault(cfg.vault) == []
    section = reportmod.format_run_section(rep, cfg.vault, NOW)
    assert 'Rolled off: "The hook rewrite is paused."' in section


def test_a_line_stays_while_its_session_is_in_the_window(cfg, fake):
    session_digest(cfg, 1)
    paused_line(cfg, "s1")
    for n in range(2, 5):
        session_digest(cfg, n)
    unrelated_waiting(cfg, 5)
    fake.script = [reply("skip"), LINES_REPLY]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert "The hook rewrite is paused." in recent_text(cfg)
    assert rep.rolled_off == []


def test_the_window_is_the_configured_number_of_sessions(cfg, fake, home):
    (home / "config.toml").write_text(
        (home / "config.toml").read_text() + "[dream]\nshort_term_sessions = 2\n"
    )
    config = configmod.load()
    session_digest(config, 1)
    paused_line(config, "s1")
    session_digest(config, 2)
    unrelated_waiting(config, 3)
    fake.script = [reply("skip"), LINES_REPLY]
    schedule.run(config, now=NOW)
    assert "The hook rewrite is paused." not in recent_text(config)


def test_time_alone_rolls_nothing_off(cfg, fake):
    """No new session of the project, no roll-off: a line at the edge of the window stays
    while only a worker's digest arrives, days on, and the day backstop leaves it too."""
    for n in range(1, 6):
        session_digest(cfg, n)
    paused_line(cfg, "s1")
    write_digest(
        cfg,
        SLUG,
        "2026-10-06-c-w6",
        facts=[short_fact("Lunch was late.", ref="s90")],
        sources=[source("s90", "w6", "Lunch was late.")],
        role="worker",
        at="2026-10-06T08:00:00Z",
    )
    fake.script = [reply("skip")]
    rep = schedule.run(cfg, now=NOW + timedelta(days=3)).reports[SLUG]
    assert "The hook rewrite is paused." in recent_text(cfg)
    assert rep.rolled_off == []
    assert rep.expired == []


def test_a_line_expires_once_its_days_are_spent(cfg, fake):
    """The day backstop (BUILD-PLAN F47): counted in sessions, a line of a project that
    stops having sessions never leaves by count, so what time has left behind expires the
    same way roll-off does."""
    session_digest(cfg, 1)
    paused_line(cfg, "s1", at=NOW - timedelta(days=10))
    unrelated_waiting(cfg, 6)
    fake.script = [reply("skip"), LINES_REPLY]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert "The hook rewrite is paused." not in recent_text(cfg)
    assert [e.statement for e in rep.expired] == ["The hook rewrite is paused."]
    assert rep.rolled_off == []
    assert lint.lint_vault(cfg.vault) == []
    section = reportmod.format_run_section(rep, cfg.vault, NOW)
    assert 'Expired: "The hook rewrite is paused."' in section


def test_a_line_its_days_are_not_yet_spent_stays(cfg, fake):
    session_digest(cfg, 1)
    paused_line(cfg, "s1", at=NOW - timedelta(days=7))
    unrelated_waiting(cfg, 6)
    fake.script = [reply("skip"), LINES_REPLY]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert "The hook rewrite is paused." in recent_text(cfg)
    assert rep.expired == []


def test_the_backstop_is_the_configured_number_of_days(cfg, fake, home):
    (home / "config.toml").write_text(
        (home / "config.toml").read_text() + "[dream]\nshort_term_days = 10\n"
    )
    config = configmod.load()
    session_digest(config, 1)
    paused_line(config, "s1", at=NOW - timedelta(days=10))
    unrelated_waiting(config, 6)
    fake.script = [reply("skip"), LINES_REPLY]
    schedule.run(config, now=NOW)
    assert "The hook rewrite is paused." in recent_text(config)


def test_a_rolled_off_line_is_still_found_in_its_digest(cfg, fake):
    session_digest(
        cfg,
        1,
        facts=[short_fact("The hook rewrite is paused.", ref="s10")],
        sources=[source("s10", "s1", "The hook rewrite is paused.")],
    )
    paused_line(cfg, "s1")
    for n in range(2, 6):
        session_digest(cfg, n)
    unrelated_waiting(cfg, 6)
    fake.script = [reply("skip"), LINES_REPLY]
    schedule.run(cfg, now=NOW)
    assert "The hook rewrite is paused." not in recent_text(cfg)
    hits = search.search(cfg, "hook rewrite paused", all_projects=True)
    assert any("digests/" in hit.path for hit in hits)


def test_a_rolled_off_line_takes_its_note_to_the_archive(cfg, fake):
    session_digest(cfg, 1)
    seed_recent(
        cfg,
        [
            "- The hook rewrite is paused. [observed] 2026-10-01 [^r0]",
            "",
            "> [!conflict] 2026-10-01",
            "> Another session says it resumed.",
            "> My answer: it stays paused.",
        ],
        [source("r0", "s1", "The hook rewrite is paused.")],
    )
    for n in range(2, 6):
        session_digest(cfg, n)
    unrelated_waiting(cfg, 6)
    fake.script = [reply("skip"), LINES_REPLY]
    schedule.run(cfg, now=NOW)
    assert "The hook rewrite is paused." not in recent_text(cfg)
    archived = (cfg.vault / "archive" / RECENT).read_text()
    assert "My answer: it stays paused." in archived


# --------------------------------------------------------------------- to long-term memory


def test_a_line_whose_sources_span_the_window_becomes_long_term(cfg, fake):
    session_digest(cfg, 1)
    for n in range(2, 6):
        session_digest(cfg, n)
    paused_line(cfg, "s1", "s5")
    session_digest(
        cfg,
        6,
        facts=[short_fact("The hook rewrite is still paused.", ref="s60")],
        sources=[source("s60", "s6", "The hook rewrite is still paused.")],
        waiting=True,
    )
    # The session-6 fact confirms the line; the line then spans s1 to s6 and is lifted.
    fake.script = [reply("confirm", "L1"), LINES_REPLY, reply("add")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert "The hook rewrite is paused." not in recent_text(cfg)
    assert "- The hook rewrite is paused. [observed]" in overview_text(cfg)
    assert [lifted.why for lifted in rep.lifted] == ["its sources span the short-term window"]
    assert lint.lint_vault(cfg.vault) == []
    section = reportmod.format_run_section(rep, cfg.vault, NOW)
    assert 'To long-term memory: "The hook rewrite is paused."' in section


def test_three_mentions_in_two_sessions_stay_short_term(cfg, fake, home):
    """With a window of two, three mentions would reach any count of mentions; only the
    distance between sessions promotes."""
    (home / "config.toml").write_text(
        (home / "config.toml").read_text() + "[dream]\nshort_term_sessions = 2\n"
    )
    cfg = configmod.load()
    for n in range(1, 5):
        session_digest(cfg, n)
    paused_line(cfg, "s4", "s4")
    session_digest(
        cfg,
        5,
        facts=[short_fact("The hook rewrite is still paused.", ref="s60")],
        sources=[source("s60", "s5", "The hook rewrite is still paused.")],
        waiting=True,
    )
    fake.script = [reply("confirm", "L1"), LINES_REPLY]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert "The hook rewrite is paused." in recent_text(cfg)
    assert rep.lifted == []


def test_a_later_long_term_fact_lifts_the_line_it_confirms(cfg, fake):
    session_digest(cfg, 1)
    paused_line(cfg, "s1")
    long_fact = short_fact("The hook rewrite stays paused until the queue is redone.")
    long_fact["term"] = "long"
    session_digest(
        cfg,
        2,
        facts=[long_fact],
        sources=[source("s10", "s2", "The hook rewrite stays paused until the queue is redone.")],
        waiting=True,
    )
    # The recent line shares the most words with the new fact, so it is L1.
    fake.script = [reply("confirm", "L1"), LINES_REPLY, reply("add")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert "L1 (state, recent): The hook rewrite is paused." in str(fake.requests[0])
    assert "The hook rewrite is paused." not in recent_text(cfg)
    assert "- The hook rewrite is paused. [observed]" in overview_text(cfg)
    assert [lifted.why for lifted in rep.lifted] == ["a later session's long-term fact confirms it"]


def test_a_lifted_line_the_decision_skips_leaves_recent_and_is_reported(cfg, fake):
    session_digest(cfg, 1)
    for n in range(2, 6):
        session_digest(cfg, n)
    paused_line(cfg, "s1", "s6")
    unrelated_waiting(cfg, 6)
    fake.script = [reply("skip"), LINES_REPLY, reply("skip")]
    rep = schedule.run(cfg, now=NOW).reports[SLUG]
    assert "The hook rewrite is paused." not in recent_text(cfg)
    assert "The hook rewrite is paused." not in overview_text(cfg)
    assert [lifted.statement for lifted in rep.lifted] == ["The hook rewrite is paused."]
    assert "The hook rewrite is paused." in [skip.statement for skip in rep.skips]


# --------------------------------------------------------------------- the rules


def quotes(primary: bool = True) -> tuple[rules.Evidence, ...]:
    return (
        rules.Evidence(
            source="s1",
            resource="aft://session/c/s1#e1",
            origin=rules.PRIMARY if primary else rules.WORKER,
            date=TODAY,
            verified=True,
            yours=True,
        ),
    )


def a_line(statement: str = "The build runs on the GPU host.", callouts=()) -> page.Fact:
    text = (
        "---\ntype: Project\ntitle: t\ndescription: d\nsources:\n"
        "  - {id: s9, resource: 'aft://session/c/s0#e1', quote: q}\n---\n\n"
        f"## State\n\n- {statement} [observed] 2026-09-01 [^s9]\n"
    )
    return page.parse(text, Path("p.md")).facts[0]


def check(action: str, *, term: str, recent: bool, source: str = "observed", primary=True):
    fact = rules.NewFact("A fact.", "state", source, TODAY, term=term)
    context = rules.Context(
        today=TODAY,
        evidence=quotes(primary),
        run_project="p",
        page_project="p",
        target_recent=recent,
    )
    target = None if action == "add" else a_line()
    return rules.check(rules.Decision(action, "L1"), fact, target, context)


def test_rules_send_a_short_add_to_recent_without_the_gate():
    assert check("add", term="short", recent=False) == rules.apply("add")


@pytest.mark.parametrize("action", ["supersede", "conflict"])
def test_rules_never_let_a_short_fact_edit_a_long_term_line(action):
    assert check(action, term="short", recent=False) == rules.downgrade("add", "R10")


def test_rules_let_a_short_fact_supersede_a_recent_line():
    assert check("supersede", term="short", recent=True) == rules.apply("supersede")


def test_rules_let_a_short_fact_confirm_a_long_term_line():
    assert check("confirm", term="short", recent=False) == rules.apply("confirm")


def test_rules_refuse_a_short_fact_that_is_inferred():
    assert check("add", term="short", recent=False, source="inferred") == rules.reject("R10")


def test_rules_refuse_a_short_fact_from_a_worker_session():
    assert check("add", term="short", recent=False, primary=False) == rules.reject(
        rules.WORKER_RULE
    )


def test_rules_lift_a_recent_line_a_long_fact_confirms():
    assert check("confirm", term="long", recent=True) == rules.apply("lift")


# --------------------------------------------------------------------- the page and its setting


def test_lint_refuses_candidates_and_inferred_lines_on_recent(cfg):
    seed_recent(
        cfg,
        [
            "- The hook rewrite is paused. [inferred] 2026-10-01 [^r0]",
            "",
            "## Candidates",
            "",
            "- The queue may move. [observed] 2026-10-01 [^r0] (signals 1/3, sessions 1/2)",
        ],
        [source("r0", "s1", "The hook rewrite is paused.")],
    )
    messages = [f.message for f in lint.lint_vault(cfg.vault) if f.path == RECENT]
    assert any("not [inferred]" in m for m in messages)
    assert any("no Candidates section" in m for m in messages)


def test_only_a_short_term_page_gives_up_a_line(cfg):
    doc = pagewrite.PageDoc.read(cfg.vault / f"projects/{SLUG}/overview.md")
    with pytest.raises(pagewrite.WriteError):
        doc.remove_short_term(doc.page.facts[0])


def test_short_term_sessions_defaults_to_five_and_refuses_zero(home):
    assert configmod.load().settings.dream.short_term_sessions == 5
    (home / "config.toml").write_text("[dream]\nshort_term_sessions = 0\n")
    with pytest.raises(configmod.ConfigError, match="short_term_sessions"):
        configmod.load()


def test_sessions_are_counted_once_each_in_session_order(cfg):
    session_digest(cfg, 2)
    session_digest(cfg, 1)
    write_digest(cfg, SLUG, "2026-10-01-c-s1-2", facts=[], sources=[], at="2026-10-03T08:00:00Z")
    write_digest(
        cfg, SLUG, "2026-10-04-c-w1", facts=[], sources=[], role="worker", at="2026-10-04T08:00:00Z"
    )
    order = shortterm.sessions(cfg.vault, SLUG)
    assert [s.session_id for s in order] == ["s1", "s2"]


def candidate_line() -> page.Fact:
    text = (
        "---\ntype: Project\ntitle: t\ndescription: d\nsources:\n"
        "  - {id: s9, resource: 'aft://session/c/s0#e1', quote: q}\n---\n\n"
        "## Candidates\n\n- The build runs on the GPU host. [observed] 2026-09-01 [^s9]"
        " (signals 1/3, sessions 1/2)\n"
    )
    return page.parse(text, Path("p.md")).facts[0]


def test_rules_let_a_short_fact_only_confirm_a_long_term_candidate():
    """R10: confirming is all a short-term fact does to long-term memory, so a candidate it
    confirms is not carried over the gate by it."""
    fact = rules.NewFact("A fact.", "state", "observed", TODAY, term="short")
    context = rules.Context(today=TODAY, evidence=quotes(), run_project="p", page_project="p")
    outcome = rules.check(rules.Decision("confirm", "L1"), fact, candidate_line(), context)
    assert outcome == rules.apply("confirm")


def test_rules_refuse_a_short_global_fact():
    fact = rules.NewFact("A fact.", "preference", "stated", TODAY, scope="global", term="short")
    context = rules.Context(today=TODAY, evidence=quotes(), run_project="p", page_project=None)
    assert rules.check(rules.Decision("add"), fact, None, context) == rules.reject("R10")


def test_rules_hold_r4_on_recent():
    """An older quote cannot replace a short-term line any more than a long-term one."""
    fact = rules.NewFact("A fact.", "state", "observed", "2026-08-01", term="short")
    old = (
        rules.Evidence(
            source="s1",
            resource="aft://session/c/s1#e1",
            origin=rules.PRIMARY,
            date="2026-08-01",
            verified=True,
        ),
    )
    context = rules.Context(
        today=TODAY, evidence=old, run_project="p", page_project="p", target_recent=True
    )
    outcome = rules.check(rules.Decision("supersede", "L1"), fact, a_line(), context)
    assert outcome == rules.downgrade("conflict", "R4")


def test_lint_refuses_an_empty_candidates_heading_on_recent(cfg):
    seed_recent(
        cfg,
        ["- The hook rewrite is paused. [observed] 2026-10-01 [^r0]", "", "## Candidates"],
        [source("r0", "s1", "The hook rewrite is paused.")],
    )
    messages = [f.message for f in lint.lint_vault(cfg.vault) if f.path == RECENT]
    assert "short-term memory has no Candidates section" in messages


def test_a_hand_edit_counts_as_the_newest_session_dreamed_before_it(cfg):
    session_digest(cfg, 1)
    session_digest(cfg, 2)
    edit = run_git(["rev-parse", "HEAD"], cfg.vault)
    session_digest(cfg, 3, waiting=True)
    order = shortterm.sessions(cfg.vault, SLUG)
    sources = {"s9": {"resource": f"aft://edit/{edit}", "quote": "q"}}
    # Sessions 1 and 2 were dreamed in September 2026, before the edit's commit, and
    # session 3 waits: the edit is placed at session 2.
    assert shortterm.positions(a_line(), sources, order, cfg.vault) == [1]


def test_a_hand_edit_before_any_dreamed_session_comes_before_the_first(cfg):
    session_digest(cfg, 1, waiting=True)
    edit = run_git(["rev-parse", "HEAD"], cfg.vault)
    order = shortterm.sessions(cfg.vault, SLUG)
    sources = {"s9": {"resource": f"aft://edit/{edit}", "quote": "q"}}
    assert shortterm.positions(a_line(), sources, order, cfg.vault) == [-1]
