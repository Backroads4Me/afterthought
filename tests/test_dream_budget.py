"""The dream counts against the day's token budget (BUILD-PLAN F39, M18; DESIGN 13)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from aft import model, page, vault
from aft.dream import schedule
from aft.registry import Registry
from test_apply import NOW, TODAY, config_with_fake, fake, reply, seed_overview, write_digest

__all__ = ["fake"]  # the fixture, imported so pytest finds it here


def spend(cfg, tokens: int) -> None:
    """Record ``tokens`` of model use earlier today, as aft.model logs a call."""
    line = {"at": NOW.replace(hour=1).isoformat(), "prompt_tokens": tokens, "completion_tokens": 0}
    with (cfg.home / "usage.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def waiting_digest(cfg) -> None:
    write_digest(
        cfg,
        "widget",
        "2026-10-05-c-s1",
        facts=[
            {
                "statement": "The queue uses SQLite WAL mode.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/c/s1#e1",
                "quote": "The queue uses SQLite WAL mode.",
            }
        ],
    )


def test_a_spent_budget_leaves_the_digests_waiting(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    waiting_digest(cfg)
    spend(cfg, cfg.settings.work.daily_token_budget)
    run = schedule.run(cfg, now=NOW)
    assert run.over_budget == ["widget"]
    assert run.ran == []
    assert fake.requests == [], "the dream called the model with the day's budget spent"
    assert "widget" in schedule.waiting_digests(cfg.vault)


def test_a_budget_with_room_lets_the_dream_run(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    waiting_digest(cfg)
    spend(cfg, cfg.settings.work.daily_token_budget - 1)
    fake.script = [reply("add")]
    run = schedule.run(cfg, now=NOW)
    assert run.ran == ["widget"]
    assert run.over_budget == []


def _register_widget(cfg) -> None:
    """Register the widget project the way ``aft project add`` leaves it: committed, so
    the run meets no uncommitted change of the test's own making."""
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "aft: register widget")


def _seed_overview(cfg) -> None:
    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- Existing line. [observed] 2026-09-30 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "existing quote"}],
    )


def _one_fact_digest(cfg, name: str, statement: str, session: str) -> None:
    """One waiting digest of widget's, holding one stated fact whose quote is in the
    evidence archive."""
    write_digest(
        cfg,
        "widget",
        name,
        facts=[
            {
                "statement": statement,
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[{"id": "s1", "resource": f"aft://session/c/{session}#e5", "quote": statement}],
    )


def _consumed(cfg, name: str) -> str | None:
    """The ``aft.consumed`` mark of one of widget's waiting digests, if it carries one."""
    frontmatter, _, _, _ = page.split_frontmatter(
        (cfg.vault / f"digests/widget/2026/10/{name}.md").read_text()
    )
    aft = (frontmatter or {}).get("aft") or {}
    return aft.get("consumed")


def test_a_budget_that_runs_out_midproject_leaves_later_digests_waiting(initialized, home, fake):
    """A project whose digests outlast the day's budget stops at the day's last affordable
    decide call (DESIGN 13): the digest applied whole is consumed, the one whose facts were
    never decided waits, and the next run with room resumes it."""
    cfg = config_with_fake(home, initialized.vault, fake)
    _register_widget(cfg)
    _seed_overview(cfg)
    _one_fact_digest(cfg, "d1", "The queue uses SQLite WAL mode.", "2")
    _one_fact_digest(cfg, "d2", "The GPU host serves vLLM.", "3")

    # One decide call fits the day: the first digest is applied whole, and the second is
    # reached with the budget spent.
    spend(cfg, cfg.settings.work.daily_token_budget - 250)
    fake.script = [reply("add", None, "new decision")]
    run = schedule.run(cfg, now=NOW)

    assert run.ran == ["widget"]
    assert run.over_budget == []
    assert len(fake.requests) == 1, "the dream decided nothing past the day's budget"
    assert run.budget_stopped == ["widget"]
    assert run.taken == {"widget": 1}
    assert _consumed(cfg, "d1"), "the digest whose facts were all applied is consumed"
    assert _consumed(cfg, "d2") is None, "the later digest waits for the next run"
    day_report = (cfg.vault / "reports/2026/10/2026-10-05.md").read_text()
    assert "Stopped: the day's token budget ran out mid-project" in day_report

    # The next run, with room in the budget, resumes the digest that waited.
    model.reset_budget(cfg, now=NOW)
    fake.script = [reply("add", None, "resumed decision")]
    run = schedule.run(cfg, now=NOW)
    assert run.budget_stopped == []
    assert run.taken == {"widget": 1}
    assert _consumed(cfg, "d2")
    overview = (cfg.vault / "projects/widget/overview.md").read_text()
    assert "- The queue uses SQLite WAL mode. [stated] 2026-10-05" in overview
    assert "- The GPU host serves vLLM. [stated] 2026-10-05" in overview


def test_a_partially_applied_digest_is_not_marked_consumed(initialized, home, fake):
    """A digest the budget stopped inside carries no consumed mark: its undecided facts
    would be lost with one, so the whole digest waits and the next run applies it again
    from its start (DESIGN 13). The facts applied before the stop stand."""
    cfg = config_with_fake(home, initialized.vault, fake)
    _register_widget(cfg)
    _seed_overview(cfg)
    write_digest(
        cfg,
        "widget",
        "d1",
        facts=[
            {
                "statement": "The queue uses SQLite WAL mode.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            },
            {
                "statement": "The GPU host serves vLLM.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s2",
            },
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/c/2#e5",
                "quote": "The queue uses SQLite WAL mode.",
            },
            {
                "id": "s2",
                "resource": "aft://session/c/3#e5",
                "quote": "The GPU host serves vLLM.",
            },
        ],
    )

    # One decide call fits the day: the digest's first fact is applied, and its second is
    # reached with the budget spent.
    spend(cfg, cfg.settings.work.daily_token_budget - 250)
    fake.script = [reply("add", None, "new decision")]
    run = schedule.run(cfg, now=NOW)

    assert run.ran == ["widget"]
    assert len(fake.requests) == 1
    assert _consumed(cfg, "d1") is None, "a digest with facts left undecided is not consumed"
    assert run.taken == {}
    overview = (cfg.vault / "projects/widget/overview.md").read_text()
    assert "- The queue uses SQLite WAL mode. [stated] 2026-10-05" in overview
    assert "The GPU host serves vLLM." not in overview

    # The next run applies the digest again from its start: the fact that stands is
    # decided again and skipped, the undecided one is applied, and the digest is consumed
    # whole. The script is indexed from the first call of this run, so the requests the
    # last one made are dropped first.
    model.reset_budget(cfg, now=NOW)
    fake.requests.clear()
    fake.script = [reply("skip", None, "already there"), reply("add", None, "second decision")]
    run = schedule.run(cfg, now=NOW)
    assert run.budget_stopped == []
    assert run.taken == {"widget": 1}
    assert _consumed(cfg, "d1")
    assert len(fake.requests) == 2
    overview = (cfg.vault / "projects/widget/overview.md").read_text()
    assert overview.count("- The queue uses SQLite WAL mode. [stated] 2026-10-05") == 1
    assert "- The GPU host serves vLLM. [stated] 2026-10-05" in overview


def test_the_cli_says_a_run_stopped_for_the_budget(initialized, home, fake, capsys):
    """``aft dream`` names the project whose run the day's budget stopped mid-way."""
    from aft.cli import main

    cfg = config_with_fake(home, initialized.vault, fake)
    _register_widget(cfg)
    _seed_overview(cfg)
    _one_fact_digest(cfg, "d1", "The queue uses SQLite WAL mode.", "2")
    _one_fact_digest(cfg, "d2", "The GPU host serves vLLM.", "3")
    # The command runs on the real clock, so the pre-spend is dated now rather than NOW:
    # one decide call fits the day, and the second digest is reached with it spent.
    line = {
        "at": datetime.now(UTC).isoformat(),
        "prompt_tokens": cfg.settings.work.daily_token_budget - 250,
        "completion_tokens": 0,
    }
    with (cfg.home / "usage.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    fake.script = [reply("add", None, "new decision")]
    assert main(["dream"]) == 0
    out = capsys.readouterr().out
    assert "aft dream: widget: 1 digest(s) taken up" in out
    assert (
        "aft dream: widget: stopped mid-project, the day's token budget is spent;"
        " the rest of its digests wait" in out
    )
