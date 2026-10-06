"""Tests for deterministic temporal query parsing (BUILD-PLAN F67)."""

from __future__ import annotations

from datetime import UTC, datetime

from aft.temporal import parse_temporal

FIXED_NOW = datetime(2026, 10, 6, 14, 30, 0, tzinfo=UTC)  # Tuesday


def test_empty_query():
    tc = parse_temporal("", now=FIXED_NOW)
    assert tc.start is None
    assert tc.end is None
    assert tc.stripped_query == ""
    assert tc.matched_text == ""

    tc2 = parse_temporal("   ", now=FIXED_NOW)
    assert tc2.start is None
    assert tc2.end is None
    assert tc2.stripped_query == ""
    assert tc2.matched_text == ""


def test_plain_query_no_temporal():
    tc = parse_temporal("titanium bolt fastening", now=FIXED_NOW)
    assert tc.start is None
    assert tc.end is None
    assert tc.stripped_query == "titanium bolt fastening"
    assert tc.matched_text == ""


def test_relative_day_today():
    tc = parse_temporal("today", now=FIXED_NOW)
    assert tc.start == datetime(2026, 10, 6, 0, 0, 0, tzinfo=UTC)
    assert tc.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc.stripped_query == ""
    assert tc.matched_text == "today"

    tc_with_query = parse_temporal("sqlite errors today", now=FIXED_NOW)
    assert tc_with_query.start == datetime(2026, 10, 6, 0, 0, 0, tzinfo=UTC)
    assert tc_with_query.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc_with_query.stripped_query == "sqlite errors"
    assert tc_with_query.matched_text == "today"


def test_relative_day_yesterday():
    tc = parse_temporal("yesterday", now=FIXED_NOW)
    assert tc.start == datetime(2026, 10, 5, 0, 0, 0, tzinfo=UTC)
    assert tc.end == datetime(2026, 10, 5, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc.stripped_query == ""
    assert tc.matched_text == "yesterday"

    tc_leading = parse_temporal("yesterday backup completed", now=FIXED_NOW)
    assert tc_leading.stripped_query == "backup completed"
    assert tc_leading.matched_text == "yesterday"


def test_relative_modifiers_days():
    tc = parse_temporal("last 3 days", now=FIXED_NOW)
    assert tc.start == datetime(2026, 10, 3, 0, 0, 0, tzinfo=UTC)
    assert tc.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc.stripped_query == ""
    assert tc.matched_text == "last 3 days"

    tc2 = parse_temporal("disk failure past 3 days in cluster", now=FIXED_NOW)
    assert tc2.start == datetime(2026, 10, 3, 0, 0, 0, tzinfo=UTC)
    assert tc2.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc2.stripped_query == "disk failure in cluster"
    assert tc2.matched_text == "past 3 days"


def test_relative_modifiers_prepositions():
    tc = parse_temporal("errors in the last 3 days", now=FIXED_NOW)
    assert tc.start == datetime(2026, 10, 3, 0, 0, 0, tzinfo=UTC)
    assert tc.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc.stripped_query == "errors"
    assert tc.matched_text == "in the last 3 days"

    tc2 = parse_temporal("over the past week metrics", now=FIXED_NOW)
    assert tc2.start == datetime(2026, 9, 29, 0, 0, 0, tzinfo=UTC)
    assert tc2.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc2.stripped_query == "metrics"
    assert tc2.matched_text == "over the past week"


def test_relative_modifiers_weeks():
    tc = parse_temporal("past week", now=FIXED_NOW)
    assert tc.start == datetime(2026, 9, 29, 0, 0, 0, tzinfo=UTC)
    assert tc.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc.stripped_query == ""
    assert tc.matched_text == "past week"

    tc2 = parse_temporal("previous 2 weeks", now=FIXED_NOW)
    assert tc2.start == datetime(2026, 9, 22, 0, 0, 0, tzinfo=UTC)
    assert tc2.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc2.stripped_query == ""


def test_relative_modifiers_this_week_month_year():
    # 2026-10-06 is a Tuesday; Monday is 2026-10-05, Sunday is 2026-10-11
    tc_week = parse_temporal("this week", now=FIXED_NOW)
    assert tc_week.start == datetime(2026, 10, 5, 0, 0, 0, tzinfo=UTC)
    assert tc_week.end == datetime(2026, 10, 11, 23, 59, 59, 999999, tzinfo=UTC)

    tc_month = parse_temporal("this month", now=FIXED_NOW)
    assert tc_month.start == datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)
    assert tc_month.end == datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=UTC)

    tc_year = parse_temporal("this year", now=FIXED_NOW)
    assert tc_year.start == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert tc_year.end == datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)


def test_relative_modifiers_months_years():
    tc_month = parse_temporal("last 2 months", now=FIXED_NOW)
    assert tc_month.start == datetime(2026, 8, 6, 0, 0, 0, tzinfo=UTC)
    assert tc_month.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=UTC)

    tc_year = parse_temporal("past 1 year", now=FIXED_NOW)
    assert tc_year.start == datetime(2025, 10, 6, 0, 0, 0, tzinfo=UTC)
    assert tc_year.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=UTC)


def test_word_numbers():
    tc = parse_temporal("last two days", now=FIXED_NOW)
    assert tc.start == datetime(2026, 10, 4, 0, 0, 0, tzinfo=UTC)
    assert tc.end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=UTC)


def test_iso_dates_day():
    tc = parse_temporal("2026-09-28", now=FIXED_NOW)
    assert tc.start == datetime(2026, 9, 28, 0, 0, 0, tzinfo=UTC)
    assert tc.end == datetime(2026, 9, 28, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc.stripped_query == ""
    assert tc.matched_text == "2026-09-28"

    tc_embed = parse_temporal("flange tolerance 2026-09-22 specs", now=FIXED_NOW)
    assert tc_embed.start == datetime(2026, 9, 22, 0, 0, 0, tzinfo=UTC)
    assert tc_embed.end == datetime(2026, 9, 22, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc_embed.stripped_query == "flange tolerance specs"


def test_iso_dates_month():
    tc = parse_temporal("2026-09", now=FIXED_NOW)
    assert tc.start == datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
    assert tc.end == datetime(2026, 9, 30, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc.stripped_query == ""
    assert tc.matched_text == "2026-09"

    # Leap year February
    tc_leap = parse_temporal("2024-02", now=FIXED_NOW)
    assert tc_leap.start == datetime(2024, 2, 1, 0, 0, 0, tzinfo=UTC)
    assert tc_leap.end == datetime(2024, 2, 29, 23, 59, 59, 999999, tzinfo=UTC)


def test_iso_date_ranges():
    tc = parse_temporal("2026-09-01 to 2026-09-15", now=FIXED_NOW)
    assert tc.start == datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
    assert tc.end == datetime(2026, 9, 15, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc.stripped_query == ""
    assert tc.matched_text == "2026-09-01 to 2026-09-15"

    tc_from = parse_temporal("events from 2026-09-01 until 2026-09-15 log", now=FIXED_NOW)
    assert tc_from.start == datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
    assert tc_from.end == datetime(2026, 9, 15, 23, 59, 59, 999999, tzinfo=UTC)
    assert tc_from.stripped_query == "events log"


def test_since_expressions():
    # 2026-10-06 is Tuesday; "since Tuesday" resolves to 7 days ago (previous Tuesday)
    tc_tue = parse_temporal("since Tuesday", now=FIXED_NOW)
    assert tc_tue.start == datetime(2026, 9, 29, 0, 0, 0, tzinfo=UTC)
    assert tc_tue.end is None
    assert tc_tue.stripped_query == ""
    assert tc_tue.matched_text == "since Tuesday"

    tc_iso = parse_temporal("status since 2026-09-15", now=FIXED_NOW)
    assert tc_iso.start == datetime(2026, 9, 15, 0, 0, 0, tzinfo=UTC)
    assert tc_iso.end is None
    assert tc_iso.stripped_query == "status"
    assert tc_iso.matched_text == "since 2026-09-15"

    tc_iso_m = parse_temporal("since 2026-09", now=FIXED_NOW)
    assert tc_iso_m.start == datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
    assert tc_iso_m.end is None

    tc_yest = parse_temporal("since yesterday", now=FIXED_NOW)
    assert tc_yest.start == datetime(2026, 10, 5, 0, 0, 0, tzinfo=UTC)
    assert tc_yest.end is None

    tc_mod = parse_temporal("since last week", now=FIXED_NOW)
    assert tc_mod.start == datetime(2026, 9, 29, 0, 0, 0, tzinfo=UTC)
    assert tc_mod.end is None


def test_before_and_until_expressions():
    tc_iso = parse_temporal("before 2026-09-15", now=FIXED_NOW)
    assert tc_iso.start is None
    assert tc_iso.end == datetime(2026, 9, 15, 23, 59, 59, 999999, tzinfo=UTC)

    tc_yest = parse_temporal("until yesterday", now=FIXED_NOW)
    assert tc_yest.start is None
    assert tc_yest.end == datetime(2026, 10, 5, 23, 59, 59, 999999, tzinfo=UTC)


def test_standalone_weekday():
    # 2026-10-06 is Tuesday; "Tuesday" alone refers to 7 days ago
    tc = parse_temporal("Tuesday", now=FIXED_NOW)
    assert tc.start == datetime(2026, 9, 29, 0, 0, 0, tzinfo=UTC)
    assert tc.end == datetime(2026, 9, 29, 23, 59, 59, 999999, tzinfo=UTC)

    # Monday (yesterday)
    tc_mon = parse_temporal("Monday", now=FIXED_NOW)
    assert tc_mon.start == datetime(2026, 10, 5, 0, 0, 0, tzinfo=UTC)
    assert tc_mon.end == datetime(2026, 10, 5, 23, 59, 59, 999999, tzinfo=UTC)


def test_default_now_is_used():
    tc = parse_temporal("today")
    assert tc.start is not None
    assert tc.end is not None
    assert tc.start.tzinfo == UTC
    assert tc.end.tzinfo == UTC
