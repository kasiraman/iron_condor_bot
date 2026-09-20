"""
Known macro-event dates (FOMC decisions, CPI/NFP releases) and monthly options
expiration ("opex") days -- used by em_multiplier_calibration.py to segment its backtest
into "event day" vs "ordinary day" buckets, and to flag whether TODAY is a known event day
when picking which calibrated EM multiplier to use.

Opex days are COMPUTED (third Friday of the month) -- nothing to maintain there. FOMC/
CPI/NFP dates are NOT computable from a formula; they're published by the Fed/BLS a few
months to about a year ahead of time and must be kept up to date by hand below.

Sources -- re-check and extend periodically (at least 1-2x/year, whenever the next batch
of dates is published):
  - FOMC decision dates: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
    Use the SECOND day of each 2-day meeting -- that's the day of the 2:00pm rate decision
    (and, at 4 of the 8 meetings/year, the press conference) -- the actual market-moving
    event. The first day of a 2-day meeting is not included here.
  - CPI release dates:   https://www.bls.gov/schedule/news_release/cpi.htm
  - NFP (Employment Situation) release dates: https://www.bls.gov/schedule/news_release/empsit.htm
    NFP does NOT reliably fall on "the first Friday" -- holidays and scheduling shift it
    some months (e.g. 2026's July report landed on Thursday July 2, ahead of the July 4th
    holiday). Always use the BLS's actual published date, not a computed rule.

SEEDED COVERAGE (as of when this was written, Sept 2026): FOMC dates are seeded for all of
2026. CPI/NFP dates are seeded from Dec 2025 through Nov 2026. Dates before Dec 2025 are
NOT included -- a historical trading day before that will be treated as an "ordinary" day
by the backtest even if it was actually a real FOMC/CPI/NFP day. Event days are a small
fraction (roughly 5%) of any multi-year lookback, so this dilutes the "ordinary" bucket's
tail slightly but doesn't invalidate it; the "event" bucket will simply have fewer
historical samples to calibrate from until you extend the sets below further back using
the same sources above (each source publishes an archive of past dates, not just upcoming
ones). Extend FORWARD too, before this coverage window runs out, or event days will
silently stop being flagged and calibration will quietly fall back to the pooled/ordinary
distribution for what are actually event days.
"""

from datetime import date

# FOMC decision days (2nd day of each 2-day meeting) -- 2026, from the Fed's official
# calendar (https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) as of Sept 2026.
FOMC_DATES = {
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 4, 29),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 10, 28),
    date(2026, 12, 9),
}

# CPI release dates -- Dec 2025 through Nov 2026, from BLS's official schedule
# (https://www.bls.gov/schedule/news_release/cpi.htm) as of Sept 2026.
CPI_DATES = {
    date(2025, 12, 18),
    date(2026, 1, 13),
    date(2026, 2, 13),
    date(2026, 3, 11),
    date(2026, 4, 10),
    date(2026, 5, 12),
    date(2026, 6, 10),
    date(2026, 7, 14),
    date(2026, 8, 12),
    date(2026, 9, 11),
    date(2026, 10, 14),
    date(2026, 11, 10),
}

# Employment Situation (NFP) release dates -- Dec 2025 through Nov 2026, from BLS's
# official schedule (https://www.bls.gov/schedule/news_release/empsit.htm) as of Sept 2026.
NFP_DATES = {
    date(2025, 12, 16),
    date(2026, 1, 9),
    date(2026, 2, 11),
    date(2026, 3, 6),
    date(2026, 4, 3),
    date(2026, 5, 8),
    date(2026, 6, 5),
    date(2026, 7, 2),
    date(2026, 8, 7),
    date(2026, 9, 4),
    date(2026, 10, 2),
    date(2026, 11, 6),
}


def is_opex_day(d: date) -> bool:
    """Third Friday of the month -- standard monthly equity/index options expiration.
    Computed, not maintained."""
    return d.weekday() == 4 and 15 <= d.day <= 21


def is_event_day(d: date) -> bool:
    """True if `d` is a known FOMC decision day, CPI release day, NFP release day, or
    monthly opex day. See the module docstring for source/coverage caveats -- dates
    outside the seeded window below are always treated as "ordinary", even if they were
    actually real event days."""
    return d in FOMC_DATES or d in CPI_DATES or d in NFP_DATES or is_opex_day(d)


def event_labels(d: date) -> list:
    """Which specific event(s) apply to `d`, for logging -- e.g. ["FOMC"], ["CPI", "opex"]."""
    labels = []
    if d in FOMC_DATES:
        labels.append("FOMC")
    if d in CPI_DATES:
        labels.append("CPI")
    if d in NFP_DATES:
        labels.append("NFP")
    if is_opex_day(d):
        labels.append("opex")
    return labels
