"""The dream counts against the day's token budget (BUILD-PLAN F39; DESIGN 13)."""

from __future__ import annotations

import json

from aft.dream import schedule
from test_apply import NOW, TODAY, config_with_fake, fake, reply, write_digest

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
