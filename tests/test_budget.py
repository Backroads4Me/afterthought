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
    spend(cfg, datetime.now(UTC) - timedelta(seconds=1), 120, purpose="search")
    assert cli.main(["budget"]) == 0
    before = capsys.readouterr().out
    work_line, search_line, *_ = before.splitlines()
    assert work_line.startswith("aft budget: 1,234 of ")
    assert " work tokens used since 00:00Z;" in work_line
    assert search_line.startswith("aft budget: 120 of ")
    assert " search tokens used since 00:00Z;" in search_line
    cli.main(["budget", "reset"])
    capsys.readouterr()
    assert cli.main(["budget"]) == 0
    after = capsys.readouterr().out
    assert "0 of" in after and "since a reset at" in after
    assert after.count("since a reset at") == 2


def _ledger_total(cfg, since=None, purpose=None) -> int:
    """The ledger's billable total, summed from the whole file."""
    total = 0
    for line in model.usage_path(cfg).read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if purpose is not None and record.get("purpose", "work") != purpose:
            continue
        if since is not None and model._time(record.get("at")) < since:
            continue
        prompt, completion = record.get("prompt_tokens"), record.get("completion_tokens")
        if isinstance(prompt, int) and isinstance(completion, int):
            total += prompt + completion
    return total


def test_a_check_reads_the_ledger_from_its_end(cfg, monkeypatch):
    """A budget check reads the ledger back from its end only as far as the day it counts
    from (BUILD-PLAN T51), in blocks whose edges may cut a line, and answers what a read
    of the whole file answers."""
    monkeypatch.setattr(model, "LEDGER_BLOCK", 97)
    for day in reversed(range(40)):
        spend(cfg, NOON - timedelta(days=day), 100)
        spend(cfg, NOON - timedelta(days=day), 9, purpose="search")
    lines = model.usage_path(cfg).read_text().splitlines()

    tail = model._ledger_lines(model.usage_path(cfg), NOON - timedelta(hours=1))
    assert 0 < len(tail) < len(lines) / 4
    for days in (0, 1, 3, 20, 45):
        since = NOON - timedelta(days=days, hours=1)
        assert model.tokens_used(cfg, since=since) == _ledger_total(cfg, since)
        assert model.tokens_used(cfg, since=since, purpose="search") == _ledger_total(
            cfg, since, "search"
        )
    assert model.tokens_used(cfg) == _ledger_total(cfg)
    assert model.tokens_used(cfg, purpose="search") == 40 * 9
    assert model.tokens_used_today(cfg, NOON) == 100


def test_a_torn_line_does_not_end_a_read_from_the_end(cfg, monkeypatch):
    """A line that cannot be read is skipped, never taken as the line that ends the read."""
    monkeypatch.setattr(model, "LEDGER_BLOCK", 50)
    spend(cfg, NOON - timedelta(days=2), 100)
    spend(cfg, NOON, 30)
    with model.usage_path(cfg).open("a") as ledger:
        ledger.write('{"at": "not a time", "prompt_t\n')
    spend(cfg, NOON, 12)
    assert model.tokens_used_today(cfg, NOON) == 42


class _Report:
    failures = ()

    def line(self) -> str:
        return "0 swept, 0 digested"
