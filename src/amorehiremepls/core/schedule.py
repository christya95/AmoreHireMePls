"""Times of day at which the watched job boards are checked without anyone asking (`AMORE_DISCOVERY_SCHEDULE`).

The value is a comma separated list of 24 hour `HH:MM` times in the configured zone (`AMORE_TIMEZONE`). Empty means no
automatic check. Pure functions over a clock that is passed in: the worker asks which time of day is due, the Jobs page
asks when the next one is.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# A time of day that passed while the worker was not running is still made up for within this long, once.
GRACE = timedelta(hours=6)
SLOT_PREFIX = "discovery:"


def parse_times(value: str | None) -> list[tuple[int, int]]:
    """(hour, minute) pairs, earliest first, each once. An entry that is not a time of day is left out."""
    times: set[tuple[int, int]] = set()
    for entry in (value or "").split(","):
        hour, sep, minute = entry.strip().partition(":")
        if sep and hour.isdigit() and minute.isdigit() and len(minute) == 2 and int(hour) < 24 and int(minute) < 60:
            times.add((int(hour), int(minute)))
    return sorted(times)


def ignored_entries(value: str | None) -> int:
    """How many entries of the setting are not a time of day, so a typing mistake can be reported."""
    return sum(1 for entry in (value or "").split(",") if entry.strip() and not parse_times(entry))


def times_text(times: list[tuple[int, int]]) -> list[str]:
    return [f"{h:02d}:{m:02d}" for h, m in times]


def slot_key(at: datetime) -> str:
    """The name one time of day on one date is claimed under, so it starts at most one check."""
    return f"{SLOT_PREFIX}{at.strftime('%Y-%m-%dT%H:%M')}"


def due_slot(times: list[tuple[int, int]], now: datetime) -> datetime | None:
    """The latest time of day that has passed, if it passed less than GRACE ago. `now` is in the schedule's zone.

    Only the latest counts: a worker that was off through several times of day makes up for one check, not for each."""
    passed = []
    for day in (now, now - timedelta(days=1)):
        for hour, minute in times:
            at = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if at <= now:
                passed.append(at)
    latest = max(passed, default=None)
    return latest if latest is not None and now - latest < GRACE else None


def next_time(times: list[tuple[int, int]], now: datetime) -> datetime | None:
    """The next time of day after `now`, today or tomorrow. `now` is in the schedule's zone."""
    ahead = []
    for day in (now, now + timedelta(days=1)):
        for hour, minute in times:
            at = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if at > now:
                ahead.append(at)
    return min(ahead, default=None)
