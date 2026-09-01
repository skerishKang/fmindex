"""KRX trading calendar and session rules for the KOSPI index collector.

Session/schedule facts used here are documented with their source:

- Regular trading hours (정규매매시간): 09:00-15:30 KST, Monday-Friday
  (KRX official market hours; closing single-price auction 15:20-15:30).
- Markets are closed on weekends and Korean public holidays
  (KRX trading calendar).
- The final partial hour (15:00-15:30) is a *partial bucket*: its
  nominal period is 60 minutes but only 30 minutes of trading exist.
  The collector marks it ``isPartial = True`` and preserves it rather
  than padding with zeros.

Public holidays are encoded as a static set of (year, month, day)
tuples. The source of truth is the KRX official trading calendar; this
list is a best-effort snapshot and must be updated when the KRX
calendar changes (see docs/kospi-hourly-collector.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

KST = timezone(timedelta(hours=9))

#: Regular session open time (KST) — KRX 정규매매시간 09:00.
SESSION_OPEN_HOUR = 9

#: Regular session close time (KST) — KRX 정규매매시간 15:30.
SESSION_CLOSE_HOUR = 15
SESSION_CLOSE_MINUTE = 30

#: Closing single-price auction start (KST) — 15:20.
CLOSING_AUCTION_HOUR = 15
CLOSING_AUCTION_MINUTE = 20

#: Calendar data source (fail-closed scope). The KRX official trading
#: calendar is the source of truth; this module ships a validated 2026
#: snapshot only. Requesting other years fails closed instead of guessing.
CALENDAR_SOURCE = "krx-official-snapshot"

#: Version of the holiday snapshot shipped in this module.
#: Bumped when new KRX special closures (elections, year-end, etc.)
#: are reconciled with the official calendar.
CALENDAR_VERSION = "2026.2"

#: Years this calendar snapshot supports. Ranges outside these years are
#: rejected rather than guessed (no weekend-only heuristics).
SUPPORTED_CALENDAR_YEARS = (2026,)

#: Holiday snapshot: (year, month, day) tuples in KST.
#:
#: 2026 Korean public holidays (공휴일) per official government notice.
#: Lunar-calendar holidays (설날/추석/부처님오신날) are based on the
#: published 2026 lunar dates. This is a best-effort snapshot; the KRX
#: official trading calendar is the source of truth and must be
#: consulted before live backfills covering these dates.
PUBLIC_HOLIDAYS: set[Tuple[int, int, int]] = {
    # 2026
    (2026, 1, 1),    # 신정
    (2026, 2, 16),   # 설날 연휴 (대체공휴일)
    (2026, 2, 17),   # 설날
    (2026, 2, 18),   # 설날 연휴
    (2026, 3, 2),    # 삼일절 대체공휴일 (3/1이 일요일)
    (2026, 5, 5),    # 어린이날
    (2026, 5, 25),   # 부처님오신날 (음력 4/8) — 5/24 일요일 대체공휴일
    (2026, 6, 6),    # 현충일
    (2026, 8, 15),   # 광복절
    (2026, 8, 17),   # 광복절 대체공휴일 (8/15 토요일)
    (2026, 9, 24),   # 추석 연휴
    (2026, 9, 25),   # 추석
    (2026, 9, 26),   # 추석 연휴
    (2026, 10, 5),   # 개천절 대체공휴일 (10/3 토요일)
    (2026, 10, 9),   # 한글날
    (2026, 12, 25),  # 성탄절
    # --- KRX special market closures (not public holidays but KRX closes) ---
    (2026, 5, 1),    # 근로자의 날 (KRX 시장 휴장)
    (2026, 6, 3),    # 제9회 전국동시지방선거일 (KRX 선거휴장)
    (2026, 7, 17),   # 제헌절 (KRX 시장 휴장)
    (2026, 12, 31),  # 연말 시장 휴장
}


class CalendarYearError(ValueError):
    """Raised when a requested date range includes an unsupported year."""


def validate_calendar_years(from_date: str, to_date: str) -> None:
    """Fail closed when the requested range includes unsupported years.

    Args:
        from_date, to_date: dates in YYYY-MM-DD format.

    Raises:
        CalendarYearError: when any year in the inclusive range is not in
            ``SUPPORTED_CALENDAR_YEARS``.
    """
    years = set()
    for d in (from_date, to_date):
        try:
            years.add(int(str(d)[:4]))
        except (TypeError, ValueError):
            raise CalendarYearError(f"Cannot parse calendar year from {d!r}.")
    if not years <= set(SUPPORTED_CALENDAR_YEARS):
        unsupported = sorted(years - set(SUPPORTED_CALENDAR_YEARS))
        raise CalendarYearError(
            "Calendar does not support year(s) "
            f"{unsupported}; supported: {sorted(SUPPORTED_CALENDAR_YEARS)}. "
            "Requesting other years fails closed (no weekend-only guessing)."
        )


def is_trading_day(dt: datetime) -> bool:
    """Return True if ``dt`` (KST) falls on a KRX trading day.

    Weekends and the public-holiday snapshot are excluded.
    """
    if dt.weekday() >= 5:  # Saturday / Sunday
        return False
    return (dt.year, dt.month, dt.day) not in PUBLIC_HOLIDAYS


def is_session_time(dt: datetime) -> bool:
    """Return True if ``dt`` (KST) falls inside regular trading hours.

    Regular session is 09:00-15:30 KST. Timestamps at or after 15:30
    are outside the session; 15:20-15:30 is the closing auction.
    """
    t = (dt.hour, dt.minute)
    return SESSION_OPEN_HOUR <= t[0] < SESSION_CLOSE_HOUR or (
        t[0] == SESSION_CLOSE_HOUR and t[1] < SESSION_CLOSE_MINUTE
    )


def is_closing_auction(dt: datetime) -> bool:
    """Return True if ``dt`` (KST) falls inside the closing auction."""
    t = (dt.hour, dt.minute)
    return (
        t[0] == CLOSING_AUCTION_HOUR
        and CLOSING_AUCTION_MINUTE <= t[1] < SESSION_CLOSE_MINUTE
    )


def bucket_for_timestamp(ts: str) -> Optional[Tuple[datetime, bool]]:
    """Map an ISO timestamp to its hourly session bucket.

    Returns ``(bucket_start, is_partial)`` where ``bucket_start`` is a
    timezone-aware KST datetime truncated to the hour, or ``None`` when
    the timestamp is outside the regular session.

    The 15:00-15:30 bucket is partial because the regular session ends
    at 15:30 (a 60-minute nominal bucket with only 30 minutes of
    trading). Buckets before 09:00 or at/after 15:30 return None so
    out-of-session data is never merged into a regular hourly bucket.
    """
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    else:
        dt = dt.astimezone(KST)

    if not is_session_time(dt):
        return None

    bucket_start = dt.replace(minute=0, second=0, microsecond=0)
    # The final session bucket (15:00-15:30) has only 30 minutes of trading
    # in a nominal 60-minute bucket, so it is marked partial.
    is_partial = bucket_start.hour == SESSION_CLOSE_HOUR
    return bucket_start, is_partial


def previous_trading_day(dt: datetime) -> datetime:
    """Return the most recent KRX trading day strictly before ``dt`` (KST)."""
    d = dt.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    while True:
        d -= timedelta(days=1)
        if is_trading_day(d):
            return d
