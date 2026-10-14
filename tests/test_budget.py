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


def _ledger_total(cfg) -> int:
    """The ledger's whole billable total, summed from the file itself."""
    total = 0
    for line in model.usage_path(cfg).read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        prompt, completion = record.get("prompt_tokens"), record.get("completion_tokens")
        if isinstance(prompt, int) and isinstance(completion, int):
            total += prompt + completion
    return total


def test_a_check_reads_the_ledger_once_and_a_call_keeps_the_answer_fresh(cfg, monkeypatch):
    """A budget check reads the day's total from the tail of the ledger (BUILD-PLAN T51):
    the file is read once, the line a call appends is folded into what that read built,
    and the next check answers without reading the file - and answers what the file
    holds. Anything else that writes the file is read again, because the file is the
    truth and the cache beside it is only a way to read it less."""
    for day in range(40):
        spend(cfg, NOON - timedelta(days=day), 100)

    parses = []
    real_parse = model._parse_usage

    def counting_parse(path, stat):
        parses.append(path)
        return real_parse(path, stat)

    monkeypatch.setattr(model, "_parse_usage", counting_parse)

    assert model.tokens_used_today(cfg, NOON) == 100
    assert len(parses) == 1, "the check read the ledger once"

    # A call appends its line; the next check answers from the read plus the line.
    model._record_usage(cfg, NOON, {"prompt_tokens": 30, "completion_tokens": 12})
    assert model.tokens_used_today(cfg, NOON) == 142
    assert len(parses) == 1, "the check after the call read the ledger again"
    assert model.tokens_used(cfg) == _ledger_total(cfg)

    # A line anything else wrote moves the file, so the next check reads it again.
    spend(cfg, NOON, 7)
    assert model.tokens_used_today(cfg, NOON) == 149
    assert len(parses) == 2
    assert model.tokens_used(cfg) == _ledger_total(cfg)


def test_a_cache_that_is_gone_or_corrupt_is_rebuilt_from_the_file(cfg):
    """The cache beside the ledger is disposable: a check that finds it absent or
    unreadable reads the ledger and answers what the file says (BUILD-PLAN T51)."""
    spend(cfg, NOON, 500)
    spend(cfg, NOON, 40, purpose="search")
    assert model.tokens_used_today(cfg, NOON) == 500
    cache = cfg.home / model.USAGE_CACHE
    assert cache.exists()
    cache.write_text("{not the cache")
    assert model.tokens_used_today(cfg, NOON) == 500
    assert model.tokens_used_today(cfg, NOON, purpose="search") == 40
    cache.unlink()
    assert model.tokens_used_today(cfg, NOON) == 500


def test_a_check_older_than_the_kept_days_reads_the_file_whole(cfg):
    """The cache keeps the newest days' lines; a check since an older moment reads the
    ledger whole and answers what the file says (BUILD-PLAN T51)."""
    for day in range(30):
        spend(cfg, NOON - timedelta(days=day), 100)
    assert model.tokens_used(cfg, since=NOON - timedelta(days=20)) == 2_100
    assert model.tokens_used(cfg, since=NOON - timedelta(days=3)) == 400
    assert model.tokens_used(cfg) == 3_000


class _Report:
    failures = ()

    def line(self) -> str:
        return "0 swept, 0 digested"
