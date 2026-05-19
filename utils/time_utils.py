"""Time, expiry and trading-session helpers (NSE / IST).

NIFTY weekly & monthly expiry day = TUESDAY.
Monthly expiry = last Tuesday of the calendar month.
"""
from __future__ import annotations

from datetime import datetime, date, time, timedelta
from typing import List

import pytz

IST = pytz.timezone("Asia/Kolkata")

# Static list of NSE holidays. Update yearly. Kept minimal -- the live trading
# day check is also gated by market-hours and weekend logic.
NSE_HOLIDAYS_2026: List[date] = [
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 6),    # Holi
    date(2026, 3, 31),   # Eid
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 11, 9),   # Diwali Laxmi Pujan (tentative)
    date(2026, 12, 25),  # Christmas
]

NSE_HOLIDAYS = set(NSE_HOLIDAYS_2026)


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:        # Sat/Sun
        return False
    if d in NSE_HOLIDAYS:
        return False
    return True


def next_trading_day(d: date) -> date:
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def trading_sessions_between(start: date, end: date) -> int:
    """Count of trading sessions in [start, end] inclusive."""
    if start > end:
        return 0
    n, cur = 0, start
    while cur <= end:
        if is_trading_day(cur):
            n += 1
        cur += timedelta(days=1)
    return n


# ---------- NIFTY weekly expiry: TUESDAY ----------
def _coming_tuesday(d: date) -> date:
    """Tuesday on or after d."""
    diff = (1 - d.weekday()) % 7   # Mon=0, Tue=1, ...
    return d + timedelta(days=diff)


def weekly_expiry_for(d: date) -> date:
    """The weekly expiry of the week containing d (Tuesday).
    If that Tuesday is a holiday, expiry shifts to previous trading day."""
    tue = _coming_tuesday(d)
    while not is_trading_day(tue):
        tue -= timedelta(days=1)
    return tue


def nth_next_weekly_expiry(d: date, n: int) -> date:
    """nth subsequent weekly expiry from date d.
    n=1 -> current week's expiry (or next if already past), n=2 -> next week, etc."""
    base_tue = _coming_tuesday(d)
    target_tue = base_tue + timedelta(weeks=n - 1)
    # Holiday adjustment
    while not is_trading_day(target_tue):
        target_tue -= timedelta(days=1)
    return target_tue


def selected_expiry(d: date, weeks_ahead: int = 2) -> date:
    """Strategy uses 'next-to-next' week => weeks_ahead = 2."""
    return nth_next_weekly_expiry(d, weeks_ahead)


def friday_before_expiry(expiry: date) -> date:
    """Return the Friday immediately preceding `expiry` (Tuesday).
    For a Tuesday expiry that's 4 calendar days before. If that Friday is a
    holiday, walk back to the previous trading day."""
    diff = (expiry.weekday() - 4) % 7
    if diff == 0:
        diff = 7
    fri = expiry - timedelta(days=diff)
    while not is_trading_day(fri):
        fri -= timedelta(days=1)
    return fri


# ---------- Market window ----------
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def is_market_open(dt: datetime | None = None) -> bool:
    dt = dt or now_ist()
    if not is_trading_day(dt.date()):
        return False
    t = dt.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def at_or_after(dt: datetime, hhmm: str) -> bool:
    return dt.time() >= parse_hhmm(hhmm)
