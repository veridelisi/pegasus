"""
Guards evaluated BEFORE contacting SerpApi. Each returns a reason string
if the call should be skipped, or None if it's safe to proceed.

These are deliberately conservative: on any ambiguity, they block the
call rather than risk burning quota (with one exception: if the DB is
totally empty / unreadable, we proceed, since there's nothing to guard
against yet and refusing forever on a fresh checkout would be worse).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from tracker.config import (
    EXPECTED_FLIGHT,
    ISTANBUL_TZ,
    MAX_MONTHLY_CALLS,
    MIN_SECONDS_BETWEEN_CALLS,
)
from tracker.db import count_calls_this_month, get_latest_api_call_observation

STATUS_MONTHLY_LIMIT_GUARD = "MONTHLY_LIMIT_GUARD"
STATUS_SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
STATUS_TRACKING_COMPLETE = "TRACKING_COMPLETE"


def flight_departure_datetime() -> datetime:
    from tracker.config import EXPECTED_FLIGHT as ef

    naive = datetime.strptime(f"{ef.date} {ef.departure_time}", "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=ISTANBUL_TZ)


def check_tracking_complete(now: datetime = None) -> str | None:
    """Returns STATUS_TRACKING_COMPLETE if the flight has already
    departed (plus a small buffer), else None.
    """
    now = now or datetime.now(ISTANBUL_TZ)
    departure = flight_departure_datetime()
    if now >= departure:
        return STATUS_TRACKING_COMPLETE
    return None


def check_monthly_limit(conn: sqlite3.Connection, now: datetime = None) -> str | None:
    now = now or datetime.now(ISTANBUL_TZ)
    calls_this_month = count_calls_this_month(conn, now.year, now.month)
    if calls_this_month >= MAX_MONTHLY_CALLS:
        return STATUS_MONTHLY_LIMIT_GUARD
    return None


def check_duplicate(conn: sqlite3.Connection, now: datetime = None) -> str | None:
    now = now or datetime.now(ISTANBUL_TZ)
    latest = get_latest_api_call_observation(conn)
    if latest is None:
        return None
    try:
        last_checked_at = datetime.fromisoformat(latest["checked_at"])
    except (ValueError, TypeError):
        return None
    if last_checked_at.tzinfo is None:
        last_checked_at = last_checked_at.replace(tzinfo=ISTANBUL_TZ)
    elapsed = (now - last_checked_at).total_seconds()
    if elapsed < MIN_SECONDS_BETWEEN_CALLS:
        return STATUS_SKIPPED_DUPLICATE
    return None


def evaluate_guards(
    conn: sqlite3.Connection,
    now: datetime = None,
    skip_duplicate_check: bool = False,
    skip_quota_check: bool = False,
) -> str | None:
    """Runs all guards in order, returns the first blocking status, or
    None if it's safe to call SerpApi. Order matters: tracking-complete
    should win over everything else (no point discussing quota for a
    flight that already departed).
    """
    now = now or datetime.now(ISTANBUL_TZ)

    tracking_status = check_tracking_complete(now)
    if tracking_status:
        return tracking_status

    if not skip_quota_check:
        monthly_status = check_monthly_limit(conn, now)
        if monthly_status:
            return monthly_status

    if not skip_duplicate_check:
        duplicate_status = check_duplicate(conn, now)
        if duplicate_status:
            return duplicate_status

    return None
