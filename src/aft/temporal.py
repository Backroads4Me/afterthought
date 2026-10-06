"""Deterministic temporal query parsing for aft search.

Extracts natural language temporal constraints into UTC boundaries and
strips temporal phrases from search queries.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class TemporalConstraint:
    """A parsed temporal constraint and the query stripped of temporal phrases."""

    start: datetime | None
    end: datetime | None
    stripped_query: str
    matched_text: str


_WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

_WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _add_months(dt: datetime, months: int) -> datetime:
    """Add or subtract calendar months to/from a datetime."""
    year = dt.year + (dt.month - 1 + months) // 12
    month = (dt.month - 1 + months) % 12 + 1
    max_day = calendar.monthrange(year, month)[1]
    day = min(dt.day, max_day)
    return dt.replace(year=year, month=month, day=day)


def _add_years(dt: datetime, years: int) -> datetime:
    """Add or subtract calendar years to/from a datetime."""
    year = dt.year + years
    max_day = calendar.monthrange(year, dt.month)[1]
    day = min(dt.day, max_day)
    return dt.replace(year=year, day=day)


def _start_of_day(dt: datetime) -> datetime:
    """Return midnight at the beginning of the day in UTC."""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _end_of_day(dt: datetime) -> datetime:
    """Return 23:59:59.999999 at the end of the day in UTC."""
    return dt.replace(hour=23, minute=59, second=59, microsecond=999999)


def _clean_query(query: str, span: tuple[int, int]) -> str:
    """Remove matched span from query and normalize whitespace."""
    start_idx, end_idx = span
    stripped = (query[:start_idx] + " " + query[end_idx:]).strip()
    return re.sub(r"\s+", " ", stripped)


# Regular expressions for temporal parsing

# Date range: 2026-09-01 to 2026-09-15, from 2026-09-01 to 2026-09-15
_RE_RANGE = re.compile(
    r"\b(?:from\s+)?(\d{4}-\d{2}-\d{2})\s+(?:to|until|through)\s+(\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)

# Relative modifiers: (in/over/during/for the)? (last/past/previous/this) (count)? (unit)
_RE_RELATIVE_MODIFIER = re.compile(
    r"\b(?:(?:in|over|during|for)\s+(?:the\s+)?)?"
    r"(?P<mod>last|past|previous|this)\s+"
    r"(?:(?P<count>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?"
    r"(?P<unit>days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)

# Since expressions: since Tuesday, since 2026-09-28, since yesterday, since last week
_RE_SINCE_ISO = re.compile(
    r"\bsince\s+(?P<iso>\d{4}-\d{2}(?:-\d{2})?)\b",
    re.IGNORECASE,
)
_RE_SINCE_RELATIVE = re.compile(
    r"\bsince\s+(?P<rel>today|yesterday)\b",
    re.IGNORECASE,
)
_RE_SINCE_WEEKDAY = re.compile(
    r"\bsince\s+(?:(?:last|past|this)\s+)?"
    r"(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b",
    re.IGNORECASE,
)
_RE_SINCE_MODIFIER = re.compile(
    r"\bsince\s+(?P<mod>last|past|previous)\s+"
    r"(?:(?P<count>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?"
    r"(?P<unit>days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)

# Before / until expressions
_RE_BEFORE_ISO = re.compile(
    r"\b(?:before|until)\s+(?P<iso>\d{4}-\d{2}(?:-\d{2})?)\b",
    re.IGNORECASE,
)
_RE_BEFORE_RELATIVE = re.compile(
    r"\b(?:before|until)\s+(?P<rel>today|yesterday)\b",
    re.IGNORECASE,
)

# Standalone ISO dates: YYYY-MM-DD or YYYY-MM
_RE_ISO_DATE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})(?:-(?P<day>\d{2}))?\b")

# Standalone relative days: today, yesterday
_RE_RELATIVE_DAY = re.compile(
    r"\b(?P<day>today|yesterday)\b",
    re.IGNORECASE,
)

# Standalone weekday: (last/past/this)? Tuesday
_RE_WEEKDAY = re.compile(
    r"\b(?:(?P<mod>last|past|this)\s+)?"
    r"(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b",
    re.IGNORECASE,
)


def _parse_count(count_str: str | None) -> int:
    """Parse count from string or return default 1."""
    if not count_str:
        return 1
    lower = count_str.lower()
    if lower in _WORD_NUMBERS:
        return _WORD_NUMBERS[lower]
    try:
        return int(count_str)
    except ValueError:
        return 1


def _resolve_weekday_offset(cur_weekday: int, target_weekday: int, modifier: str | None) -> int:
    """Calculate how many days ago the target weekday occurred."""
    diff = (cur_weekday - target_weekday) % 7
    if modifier == "this":
        # "this <weekday>" refers to the weekday in the current week (Mon-Sun)
        return cur_weekday - target_weekday
    if diff == 0:
        return 7
    return diff


def parse_temporal(query: str, now: datetime | None = None) -> TemporalConstraint:
    """Parse temporal expressions from a query into UTC start and end bounds."""
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)

    today_start = _start_of_day(now)
    today_end = _end_of_day(now)

    # 1. Date range: YYYY-MM-DD to YYYY-MM-DD
    m_range = _RE_RANGE.search(query)
    if m_range:
        d1 = datetime.strptime(m_range.group(1), "%Y-%m-%d").replace(tzinfo=UTC)
        d2 = datetime.strptime(m_range.group(2), "%Y-%m-%d").replace(tzinfo=UTC)
        return TemporalConstraint(
            start=_start_of_day(d1),
            end=_end_of_day(d2),
            stripped_query=_clean_query(query, m_range.span()),
            matched_text=query[m_range.start() : m_range.end()],
        )

    # 2. Since ISO date
    m_since_iso = _RE_SINCE_ISO.search(query)
    if m_since_iso:
        iso_str = m_since_iso.group("iso")
        if len(iso_str) == 7:  # YYYY-MM
            dt = datetime.strptime(iso_str, "%Y-%m").replace(tzinfo=UTC)
        else:  # YYYY-MM-DD
            dt = datetime.strptime(iso_str, "%Y-%m-%d").replace(tzinfo=UTC)
        return TemporalConstraint(
            start=_start_of_day(dt),
            end=None,
            stripped_query=_clean_query(query, m_since_iso.span()),
            matched_text=query[m_since_iso.start() : m_since_iso.end()],
        )

    # 3. Since relative day (today, yesterday)
    m_since_rel = _RE_SINCE_RELATIVE.search(query)
    if m_since_rel:
        rel = m_since_rel.group("rel").lower()
        if rel == "today":
            dt = today_start
        else:
            dt = today_start - timedelta(days=1)
        return TemporalConstraint(
            start=_start_of_day(dt),
            end=None,
            stripped_query=_clean_query(query, m_since_rel.span()),
            matched_text=query[m_since_rel.start() : m_since_rel.end()],
        )

    # 4. Since weekday
    m_since_wk = _RE_SINCE_WEEKDAY.search(query)
    if m_since_wk:
        wk_str = m_since_wk.group("weekday").lower()
        target_wk = _WEEKDAYS[wk_str]
        days_ago = _resolve_weekday_offset(now.weekday(), target_wk, None)
        dt = today_start - timedelta(days=days_ago)
        return TemporalConstraint(
            start=_start_of_day(dt),
            end=None,
            stripped_query=_clean_query(query, m_since_wk.span()),
            matched_text=query[m_since_wk.start() : m_since_wk.end()],
        )

    # 5. Since modifier: since last week, since past 3 days
    m_since_mod = _RE_SINCE_MODIFIER.search(query)
    if m_since_mod:
        count = _parse_count(m_since_mod.group("count"))
        unit = m_since_mod.group("unit").lower()
        if unit.startswith("day"):
            dt = today_start - timedelta(days=count)
        elif unit.startswith("week"):
            dt = today_start - timedelta(days=7 * count)
        elif unit.startswith("month"):
            dt = _add_months(today_start, -count)
        else:  # year
            dt = _add_years(today_start, -count)
        return TemporalConstraint(
            start=_start_of_day(dt),
            end=None,
            stripped_query=_clean_query(query, m_since_mod.span()),
            matched_text=query[m_since_mod.start() : m_since_mod.end()],
        )

    # 6. Before / until ISO
    m_before_iso = _RE_BEFORE_ISO.search(query)
    if m_before_iso:
        iso_str = m_before_iso.group("iso")
        if len(iso_str) == 7:  # YYYY-MM
            parts = iso_str.split("-")
            y, m = int(parts[0]), int(parts[1])
            max_d = calendar.monthrange(y, m)[1]
            dt = datetime(y, m, max_d, tzinfo=UTC)
        else:  # YYYY-MM-DD
            dt = datetime.strptime(iso_str, "%Y-%m-%d").replace(tzinfo=UTC)
        return TemporalConstraint(
            start=None,
            end=_end_of_day(dt),
            stripped_query=_clean_query(query, m_before_iso.span()),
            matched_text=query[m_before_iso.start() : m_before_iso.end()],
        )

    # 7. Before / until relative
    m_before_rel = _RE_BEFORE_RELATIVE.search(query)
    if m_before_rel:
        rel = m_before_rel.group("rel").lower()
        if rel == "today":
            dt = today_end
        else:
            dt = _end_of_day(today_start - timedelta(days=1))
        return TemporalConstraint(
            start=None,
            end=dt,
            stripped_query=_clean_query(query, m_before_rel.span()),
            matched_text=query[m_before_rel.start() : m_before_rel.end()],
        )

    # 8. Relative modifiers: last/past/previous/this <count> day(s)/week(s)/month(s)/year(s)
    m_rel_mod = _RE_RELATIVE_MODIFIER.search(query)
    if m_rel_mod:
        mod = m_rel_mod.group("mod").lower()
        count = _parse_count(m_rel_mod.group("count"))
        unit = m_rel_mod.group("unit").lower()

        if mod in ("last", "past", "previous"):
            if unit.startswith("day"):
                start_dt = today_start - timedelta(days=count)
            elif unit.startswith("week"):
                start_dt = today_start - timedelta(days=7 * count)
            elif unit.startswith("month"):
                start_dt = _add_months(today_start, -count)
            else:  # year
                start_dt = _add_years(today_start, -count)
            end_dt = today_end
        else:  # "this"
            if unit.startswith("day"):
                start_dt = today_start
                end_dt = today_end
            elif unit.startswith("week"):
                monday = (now - timedelta(days=now.weekday())).date()
                start_dt = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)
                sunday = monday + timedelta(days=6)
                end_dt = _end_of_day(datetime(sunday.year, sunday.month, sunday.day, tzinfo=UTC))
            elif unit.startswith("month"):
                start_dt = datetime(now.year, now.month, 1, tzinfo=UTC)
                last_day = calendar.monthrange(now.year, now.month)[1]
                end_dt = _end_of_day(datetime(now.year, now.month, last_day, tzinfo=UTC))
            else:  # year
                start_dt = datetime(now.year, 1, 1, tzinfo=UTC)
                end_dt = _end_of_day(datetime(now.year, 12, 31, tzinfo=UTC))

        return TemporalConstraint(
            start=_start_of_day(start_dt),
            end=end_dt,
            stripped_query=_clean_query(query, m_rel_mod.span()),
            matched_text=query[m_rel_mod.start() : m_rel_mod.end()],
        )

    # 9. Standalone relative day: today, yesterday
    m_rel_day = _RE_RELATIVE_DAY.search(query)
    if m_rel_day:
        rel = m_rel_day.group("day").lower()
        if rel == "today":
            start_dt = today_start
            end_dt = today_end
        else:
            yesterday = today_start - timedelta(days=1)
            start_dt = yesterday
            end_dt = _end_of_day(yesterday)
        return TemporalConstraint(
            start=start_dt,
            end=end_dt,
            stripped_query=_clean_query(query, m_rel_day.span()),
            matched_text=query[m_rel_day.start() : m_rel_day.end()],
        )

    # 10. Standalone ISO date: YYYY-MM-DD or YYYY-MM
    m_iso = _RE_ISO_DATE.search(query)
    if m_iso:
        y = int(m_iso.group("year"))
        m = int(m_iso.group("month"))
        d_str = m_iso.group("day")
        if 1 <= m <= 12:
            if d_str:
                d = int(d_str)
                max_d = calendar.monthrange(y, m)[1]
                if 1 <= d <= max_d:
                    dt = datetime(y, m, d, tzinfo=UTC)
                    return TemporalConstraint(
                        start=_start_of_day(dt),
                        end=_end_of_day(dt),
                        stripped_query=_clean_query(query, m_iso.span()),
                        matched_text=query[m_iso.start() : m_iso.end()],
                    )
            else:
                max_d = calendar.monthrange(y, m)[1]
                start_dt = datetime(y, m, 1, tzinfo=UTC)
                end_dt = _end_of_day(datetime(y, m, max_d, tzinfo=UTC))
                return TemporalConstraint(
                    start=start_dt,
                    end=end_dt,
                    stripped_query=_clean_query(query, m_iso.span()),
                    matched_text=query[m_iso.start() : m_iso.end()],
                )

    # 11. Standalone weekday: Tuesday, last Tuesday
    m_wk = _RE_WEEKDAY.search(query)
    if m_wk:
        wk_str = m_wk.group("weekday").lower()
        mod = (m_wk.group("mod") or "").lower()
        target_wk = _WEEKDAYS[wk_str]
        days_ago = _resolve_weekday_offset(now.weekday(), target_wk, mod if mod else None)
        target_date = today_start - timedelta(days=days_ago)
        return TemporalConstraint(
            start=_start_of_day(target_date),
            end=_end_of_day(target_date),
            stripped_query=_clean_query(query, m_wk.span()),
            matched_text=query[m_wk.start() : m_wk.end()],
        )

    return TemporalConstraint(
        start=None,
        end=None,
        stripped_query=query.strip(),
        matched_text="",
    )
