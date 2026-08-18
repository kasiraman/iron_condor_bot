"""
Shared time/date math for the weekend (Friday -> Monday) iron condor strategy.

Two different "T" (year-fraction-to-close) conventions are needed here, and conflating
them is the main way this strategy could go subtly wrong:

  1. CALENDAR T (year_fraction_to_close_calendar) -- real elapsed time, weekend included.
     Use this for solving IV off the ATM straddle and for pricing the net credit. The
     market doesn't stop pricing time value over a weekend -- a Monday-expiring option
     quoted on Friday already has ~2.5 extra calendar days of time value baked into its
     price relative to a same-day 0DTE option would, and using a T that ignores the
     weekend would produce a systematically-wrong (too high) IV solve from real Friday
     quotes.

  2. TRADING-HOURS T (year_fraction_to_close_trading_hours) -- only real trading-session
     time, weekend (and any market holiday) excluded entirely. Use this ONLY for sizing
     the expected move / strike width (EM = spot * iv * sqrt(T)). The underlying can only
     actually move during real trading sessions -- a Friday-afternoon-to-Monday-close hold
     has roughly one trading day's worth of new price-movement time ahead of it, not the
     ~3 calendar days a weekend spans, even though the option's price reflects the full
     ~3 calendar days of theta decay. Sizing strikes off the calendar T here would make
     them wider (safer) than the real trading-day price risk actually justifies; sizing
     them off the trading-hours T instead is what's intended to capture the weekend-decay
     edge described in the README's "Weekend strategy" section -- at the cost of tighter
     strikes carrying more risk of being touched if a real weekend gap does happen, which
     this strategy cannot monitor or react to since markets are closed all weekend.

Both functions are driven by Alpaca's real trading calendar (see get_trading_calendar())
rather than guessing at weekdays, so market holidays (e.g. a Monday that's actually a
holiday, which pushes the real expiration out to Tuesday) are handled correctly
automatically, without this module needing its own holiday list.
"""

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from alpaca.trading.requests import GetCalendarRequest

TIMEZONE = ZoneInfo("America/New_York")


def get_trading_calendar(trade_client, start: date, end: date):
    """Returns Alpaca's real trading calendar entries (each with .date, .open, .close)
    for every actual trading session between start and end (inclusive). Non-trading days
    (weekends, market holidays) simply don't appear in the result -- callers never need
    to special-case holidays themselves."""
    req = GetCalendarRequest(start=start, end=end)
    return trade_client.get_calendar(req)


def next_trading_session(trade_client, after: date, days_ahead: int = 10):
    """Finds the next real trading session strictly after `after` -- e.g. the Monday (or
    Tuesday, if Monday turns out to be a market holiday) that a Friday entry should
    expire on. Raises if none is found within `days_ahead` days (should only happen if
    Alpaca's calendar data itself has a gap)."""
    calendar = get_trading_calendar(trade_client, after, after + timedelta(days=days_ahead))
    for session in calendar:
        if session.date > after:
            return session
    raise RuntimeError(f"No trading session found within {days_ahead} days after {after}.")


def session_open_dt(session) -> datetime:
    """Returns an aware America/New_York datetime for when this calendar session opens.

    NOTE: alpaca-py's Calendar model already parses the raw API's separate "date" +
    "open"/"close" (HH:MM) strings into full (naive) datetime objects for `.open`/
    `.close` -- so this just attaches the America/New_York tzinfo, it does NOT need (and
    must not do) a datetime.combine(session.date, session.open) since session.open is
    already a datetime, not a bare time. Uses the session's own reported open/close
    rather than assuming 9:30/4:00, so a real early-close half day is handled correctly
    if one is ever in the window."""
    return session.open.replace(tzinfo=TIMEZONE)


def session_close_dt(session) -> datetime:
    return session.close.replace(tzinfo=TIMEZONE)


def year_fraction_to_close_calendar(now: datetime, close_dt: datetime) -> float:
    """Real calendar time remaining until `close_dt`, as a year fraction (ACT/365 day
    count) -- the same convention 0dte_iron_condor_bot.py's year_fraction_to_close() uses
    for a same-day close, just generalized to an arbitrary future close_dt so it also
    works for a Friday-to-Monday span."""
    seconds_remaining = max((close_dt - now).total_seconds(), 1.0)
    return (seconds_remaining / 86400.0) / 365.0


def year_fraction_to_close_trading_hours(now: datetime, close_dt: datetime, calendar) -> float:
    """Time remaining until `close_dt`, counting ONLY real trading-session seconds and
    excluding weekend/overnight/holiday dead time entirely.

    `calendar` must be the list of Alpaca calendar sessions spanning from `now`'s date
    through `close_dt`'s date (inclusive) -- e.g. from get_trading_calendar(now.date(),
    close_dt.date()). Any date not present in `calendar` (weekends, holidays) contributes
    zero seconds automatically, since this only sums seconds for sessions it's actually
    given.

    Example: a Friday 9:31am ET entry with `calendar` = [Friday session, Monday session]
    and close_dt = Monday 4:00pm ET counts (Friday 4:00pm - Friday 9:31am) + (Monday
    4:00pm - Monday 9:30am) = ~6.48 + ~6.50 = ~12.98 trading hours -- NOT the ~78.5
    calendar hours year_fraction_to_close_calendar() would count for the same span.
    """
    total_seconds = 0.0
    for session in calendar:
        o, c = session_open_dt(session), session_close_dt(session)
        seg_start = max(now, o)
        seg_end = min(close_dt, c)
        if seg_end > seg_start:
            total_seconds += (seg_end - seg_start).total_seconds()
    total_seconds = max(total_seconds, 1.0)
    return (total_seconds / 86400.0) / 365.0
