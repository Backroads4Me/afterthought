"""Resetting the daily token budgets and counting by purpose (BUILD-PLAN F40, F86).

A reset starts today's counts again from now without touching the usage ledger, and
midnight UTC still starts the next day as before.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from aft import cli, model

NOON = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def spend(cfg, at: datetime, tokens: int, purpose: str | None = None) -> None:
    line = {"at": at.isoformat(), "prompt_tokens": tokens, "completion_tokens": 0}
    if purpose is not None:
        line["purpose"] = purpose
    with model.usage_path(cfg).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def test_a_reset_starts_the_count_again_and_keeps_the_ledger(cfg):
    spend(cfg, NOON - timedelta(hours=2), 600_000)
    ledger = model.usage_path(cfg).read_text()
    assert model.tokens_used_today(cfg, NOON) == 600_000
    model.reset_budget(cfg, NOON)
    spend(cfg, NOON + timedelta(minutes=5), 1_000)
    assert model.tokens_used_today(cfg, NOON + timedelta(minutes=10)) == 1_000
    assert model.usage_path(cfg).read_text().startswith(ledger), "a reset rewrote the ledger"
    assert model.tokens_used(cfg) == 601_000


def test_midnight_still_starts_the_next_day(cfg):
    model.reset_budget(cfg, NOON)
    tomorrow = NOON + timedelta(days=1)
    spend(cfg, tomorrow.replace(hour=0, minute=30), 2_000)
    spend(cfg, NOON + timedelta(hours=11), 5_000)  # 23:00 the day before
    assert model.budget_day_start(cfg, tomorrow) == tomorrow.replace(hour=0)
    assert model.tokens_used_today(cfg, tomorrow) == 2_000


def test_a_line_with_no_purpose_counts_as_work(cfg):
    """Ledger lines written before F86 name no purpose; they are work lines."""
    spend(cfg, NOON, 500)
    assert model.tokens_used_today(cfg, NOON) == 500
    assert model.tokens_used_today(cfg, NOON, purpose="work") == 500
    assert model.tokens_used_today(cfg, NOON, purpose="search") == 0


def test_purposes_count_separately(cfg):
    """Search tokens leave the work count untouched and the reverse (BUILD-PLAN F86)."""
    spend(cfg, NOON, 500, purpose="search")
    spend(cfg, NOON, 300, purpose="work")
    assert model.tokens_used_today(cfg, NOON, purpose="search") == 500
    assert model.tokens_used_today(cfg, NOON, purpose="work") == 300
    assert model.tokens_used(cfg) == 800


def test_a_reset_restarts_both_counts(cfg):
    """One reset is the start of both budgets' day (BUILD-PLAN F86)."""
    spend(cfg, NOON - timedelta(hours=1), 600_000, purpose="work")
    spend(cfg, NOON - timedelta(minutes=30), 4_000, purpose="search")
    model.reset_budget(cfg, NOON)
    assert model.tokens_used_today(cfg, NOON, purpose="work") == 0
    assert model.tokens_used_today(cfg, NOON, purpose="search") == 0


def test_work_runs_again_after_a_reset(cfg, monkeypatch, capsys):
    budget = cfg.settings.work.daily_token_budget
    spend(cfg, datetime.now(UTC) - timedelta(seconds=1), budget)
    assert model.tokens_used_today(cfg) >= budget
    assert cli.main(["budget", "reset"]) == 0
    assert model.tokens_used_today(cfg) == 0
    ran = []
    monkeypatch.setattr("aft.work.run", lambda config: ran.append(True) or _Report())
    monkeypatch.setattr("aft.cli._dream", lambda config: None)
    assert cli.main(["run"]) == 0
    assert ran == [True]
    assert "the day's budget was reset at" in capsys.readouterr().out


def test_budget_reports_use_before_and_after_a_reset(cfg, capsys):
    spend(cfg, datetime.now(UTC) - timedelta(seconds=1), 1_234)
    assert cli.main(["budget"]) == 0
    before = capsys.readouterr().out
    assert "1,234 of" in before and "since 00:00Z" in before
    cli.main(["budget", "reset"])
    capsys.readouterr()
    assert cli.main(["budget"]) == 0
    after = capsys.readouterr().out
    assert "0 of" in after and "since a reset at" in after


class _Report:
    failures = ()

    def line(self) -> str:
        return "0 swept, 0 digested"
