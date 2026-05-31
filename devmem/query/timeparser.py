"""Natural language time expression parser → (start_datetime, end_datetime)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from dateutil import parser as dateutil_parser
from dateutil.relativedelta import relativedelta

# Mapping of day-name to weekday integer (Monday = 0)
_WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

DateRange = tuple[datetime, datetime]


def parse_time_expression(text: str, now: Optional[datetime] = None) -> Optional[DateRange]:
    """
    Parse a natural language time expression from *text* and return a
    (start, end) tuple of timezone-aware UTC datetimes, or None if no
    recognisable expression was found.

    The returned range is inclusive on both ends.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    text_lower = text.lower().strip()

    # --- Exact patterns ---

    # "today"
    if re.search(r"\btoday\b", text_lower):
        return _day_range(now)

    # "yesterday"
    if re.search(r"\byesterday\b", text_lower):
        return _day_range(now - timedelta(days=1))

    # "this week"
    if re.search(r"\bthis\s+week\b", text_lower):
        monday = now - timedelta(days=now.weekday())
        start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        return (start, now)

    # "last week"
    if re.search(r"\blast\s+week\b", text_lower):
        last_monday = now - timedelta(days=now.weekday() + 7)
        start = last_monday.replace(hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=6)).replace(hour=23, minute=59, second=59)
        return (start, end)

    # "this month"
    if re.search(r"\bthis\s+month\b", text_lower):
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return (start, now)

    # "last N days" / "past N days"
    m = re.search(r"\b(?:last|past)\s+(\d+)\s+days?\b", text_lower)
    if m:
        n = int(m.group(1))
        start = (now - timedelta(days=n)).replace(hour=0, minute=0, second=0, microsecond=0)
        return (start, now)

    # "last N weeks"
    m = re.search(r"\b(?:last|past)\s+(\d+)\s+weeks?\b", text_lower)
    if m:
        n = int(m.group(1))
        start = (now - timedelta(weeks=n)).replace(hour=0, minute=0, second=0, microsecond=0)
        return (start, now)

    # "last N hours"
    m = re.search(r"\b(?:last|past)\s+(\d+)\s+hours?\b", text_lower)
    if m:
        n = int(m.group(1))
        start = now - timedelta(hours=n)
        return (start, now)

    # "last <weekday>" e.g. "last Tuesday"
    m = re.search(r"\blast\s+(" + "|".join(_WEEKDAY_MAP) + r")\b", text_lower)
    if m:
        target_wd = _WEEKDAY_MAP[m.group(1)]
        days_back = (now.weekday() - target_wd) % 7
        if days_back == 0:
            days_back = 7  # "last Tuesday" means the one before this week
        target = now - timedelta(days=days_back)
        return _day_range(target)

    # "on <weekday>" / "<weekday>" alone
    m = re.search(r"\bon\s+(" + "|".join(_WEEKDAY_MAP) + r")\b", text_lower)
    if not m:
        m = re.search(r"\b(" + "|".join(_WEEKDAY_MAP) + r")\b", text_lower)
    if m:
        target_wd = _WEEKDAY_MAP[m.group(1)]
        days_back = (now.weekday() - target_wd) % 7
        if days_back == 0 and now.hour < 6:
            days_back = 7
        if days_back == 0:
            return _day_range(now)
        target = now - timedelta(days=days_back)
        return _day_range(target)

    # "<Month> <day>" e.g. "May 15"
    m = re.search(
        r"\b(" + "|".join(_MONTH_MAP) + r")\s+(\d{1,2})\b",
        text_lower,
    )
    if m:
        month = _MONTH_MAP[m.group(1)]
        day = int(m.group(2))
        year = now.year
        try:
            target = datetime(year, month, day, tzinfo=timezone.utc)
            if target > now:
                target = datetime(year - 1, month, day, tzinfo=timezone.utc)
            return _day_range(target)
        except ValueError:
            pass

    # YYYY-MM-DD
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text_lower)
    if m:
        try:
            target = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return _day_range(target)
        except ValueError:
            pass

    # Fallback: try dateutil
    try:
        parsed = dateutil_parser.parse(text, fuzzy=True, default=now)
        parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        return _day_range(parsed.astimezone(timezone.utc))
    except Exception:
        pass

    return None


def humanise_range(start: datetime, end: datetime, now: Optional[datetime] = None) -> str:
    """Return a short human-readable description of a date range."""
    if now is None:
        now = datetime.now(timezone.utc)

    today = now.date()
    start_date = start.date()
    end_date = end.date()

    if start_date == today and end_date == today:
        return "today"
    if start_date == (today - timedelta(days=1)) and end_date == (today - timedelta(days=1)):
        return "yesterday"

    start_str = start.strftime("%b %-d")
    end_str = end.strftime("%b %-d")
    if start_str == end_str:
        return start_str
    return f"{start_str} – {end_str}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _day_range(dt: datetime) -> DateRange:
    """Return (start-of-day, end-of-day) for the given datetime."""
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return (start, end)


def to_iso(dt: datetime) -> str:
    """Return an ISO 8601 UTC string."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
