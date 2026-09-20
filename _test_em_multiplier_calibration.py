"""Ad-hoc test for em_multiplier_calibration.py -- synthetic SPY-like bars with a KNOWN
ratio distribution, verifying the percentile calibration recovers it, that event/ordinary
segmentation works, and that the min-samples fallback chain behaves. Not part of the
deployed bot; run directly with `python3 _test_em_multiplier_calibration.py`.
"""
import math
import random
import statistics
from datetime import date, timedelta
from unittest.mock import MagicMock

from alpaca.data.enums import DataFeed

import em_multiplier_calibration as calib
from event_calendar import is_event_day, FOMC_DATES

random.seed(42)


class FakeBar:
    def __init__(self, d, open_, close):
        self.timestamp = MagicMock()
        self.timestamp.date.return_value = d
        self.open = open_
        self.close = close


def make_bars(start, n_days, base_vol_pct, event_dates, event_vol_multiplier):
    """Builds n_days of synthetic daily bars. Ordinary days: intraday return ~ N(0, base_vol_pct).
    Event days: intraday return ~ N(0, base_vol_pct * event_vol_multiplier) -- a REAL, KNOWN
    difference in volatility regime that a correct calibration should detect."""
    bars = []
    price = 500.0
    d = start
    made = 0
    while made < n_days:
        if d.weekday() < 5:  # weekdays only, like real trading days
            vol = base_vol_pct * event_vol_multiplier if d in event_dates else base_vol_pct
            ret = random.gauss(0, vol)
            open_ = price
            close = price * (1 + ret)
            bars.append(FakeBar(d, open_, close))
            price = close
            made += 1
        d += timedelta(days=1)
    return bars


def test_calibration_recovers_known_percentile():
    """With NO event days at all (pure ordinary regime), the calibrated multiplier at the
    XXth percentile should be close to the theoretical z-score for that percentile, since
    trailing_vol tracks the (here, constant) true vol closely for large samples."""
    from scipy.stats import norm

    start = date(2023, 1, 2)
    bars = make_bars(start, 600, base_vol_pct=0.01, event_dates=set(), event_vol_multiplier=1.0)
    # "Today" is the day after the last synthetic bar, an ordinary day (no event seeded there).
    today = bars[-1].timestamp.date() + timedelta(days=1)
    while today.weekday() >= 5 or is_event_day(today):
        today += timedelta(days=1)

    mock_client = MagicMock()
    mock_client.get_stock_bars.return_value = {"SPY": bars}

    mult, n, is_event, detail = calib.calibrate_em_multiplier(
        mock_client, "SPY", today, lookback_days=1000, vol_window=20,
        target_percentile=95.0, min_samples=20, feed=DataFeed.IEX,
    )
    assert mult is not None, f"expected a calibrated multiplier, got detail={detail!r}"
    assert not is_event
    assert detail == "ordinary"
    # abs(N(0,1)) at the 95th percentile is ~1.645 (half-normal / folded normal).
    expected = norm.ppf(0.5 + 0.95 / 2)  # 95th percentile of |Z|
    assert abs(mult - expected) < 0.25, f"calibrated={mult:.3f} vs theoretical~{expected:.3f}"
    print(f"PASS: recovered multiplier {mult:.3f} vs theoretical {expected:.3f} (n={n})")


def test_event_days_calibrate_wider_than_ordinary():
    """Seed event days with genuinely higher realized vol (2x). The event bucket's
    calibrated multiplier, evaluated against the (correctly-scaled) trailing vol from
    ordinary days just before it, should come out noticeably wider than the ordinary
    bucket's -- proving the event/ordinary split actually does something."""
    start = date(2025, 6, 2)  # range covers all of 2026's seeded FOMC/CPI/NFP dates (see event_calendar.py)
    end = start + timedelta(days=1000)
    # Inflate vol on EVERY day is_event_day() would flag (FOMC/CPI/NFP/opex) -- not just
    # FOMC -- so the synthetic scenario matches what the calibration code actually checks.
    event_dates_in_range = set()
    d = start
    while d <= end:
        if is_event_day(d):
            event_dates_in_range.add(d)
        d += timedelta(days=1)
    assert len(event_dates_in_range) >= 4, "need several seeded event dates within the synthetic range"

    bars = make_bars(start, 900, base_vol_pct=0.01, event_dates=event_dates_in_range, event_vol_multiplier=2.5)
    today = bars[-1].timestamp.date() + timedelta(days=1)
    while today.weekday() >= 5 or is_event_day(today):
        today += timedelta(days=1)

    mock_client = MagicMock()
    mock_client.get_stock_bars.return_value = {"SPY": bars}

    mult_ordinary, n_ord, is_event_today, detail_ord = calib.calibrate_em_multiplier(
        mock_client, "SPY", today, lookback_days=1200, vol_window=20,
        target_percentile=95.0, min_samples=20, feed=DataFeed.IEX,
    )
    assert not is_event_today and detail_ord == "ordinary"

    # Now call the real function with "today" set to one of the seeded event days itself
    # (picked with enough trailing history before it), to test the actual event-bucket path.
    candidate_event_days = sorted(d for d in event_dates_in_range if (d - start).days >= 400)
    assert candidate_event_days, "need a seeded event date with enough trailing history"
    an_event_day = candidate_event_days[len(candidate_event_days) // 2]
    mult_event, n_evt, is_event_flag, detail_evt = calib.calibrate_em_multiplier(
        mock_client, "SPY", an_event_day, lookback_days=1200, vol_window=20,
        target_percentile=95.0, min_samples=4, feed=DataFeed.IEX,
    )
    assert is_event_flag, f"{an_event_day} should have been flagged as an event day"
    assert mult_event is not None, f"expected a calibrated event-day multiplier, got detail={detail_evt!r}"
    print(f"ordinary-bucket multiplier: {mult_ordinary:.3f} (n={n_ord})")
    print(f"event-bucket multiplier ({an_event_day}): {mult_event:.3f} (n={n_evt}, detail={detail_evt!r})")
    assert mult_event > mult_ordinary, (
        f"expected the event-day calibrated multiplier ({mult_event:.3f}) to exceed the "
        f"ordinary-day one ({mult_ordinary:.3f}) given the seeded 2.5x event-day vol"
    )

    # Direct bucket comparison (re-deriving what the function computes internally) --
    # this is the real assertion: event days' ratios should run higher than ordinary days'.
    intraday_returns = [(float(b.close) - float(b.open)) / float(b.open) for b in bars]
    event_ratios, ordinary_ratios = [], []
    for i in range(20, len(bars)):
        trailing = intraday_returns[i - 20:i]
        vol = statistics.stdev(trailing)
        if vol <= 0:
            continue
        predicted = float(bars[i].open) * vol
        realized = abs(float(bars[i].close) - float(bars[i].open))
        ratio = realized / predicted
        (event_ratios if is_event_day(bars[i].timestamp.date()) else ordinary_ratios).append(ratio)

    assert len(event_ratios) >= 4, f"expected several event-day samples, got {len(event_ratios)}"
    p95_event = calib._percentile(event_ratios, 95.0)
    p95_ordinary = calib._percentile(ordinary_ratios, 95.0)
    assert p95_event > p95_ordinary * 1.3, (
        f"expected event-day 95th percentile ({p95_event:.2f}) to run meaningfully higher "
        f"than ordinary-day ({p95_ordinary:.2f}) given the seeded 2.5x vol multiplier"
    )
    print(f"PASS: event-day p95={p95_event:.3f} > ordinary-day p95={p95_ordinary:.3f} (n_event={len(event_ratios)})")


def test_insufficient_data_falls_back_to_none():
    bars = make_bars(date(2026, 8, 1), 15, base_vol_pct=0.01, event_dates=set(), event_vol_multiplier=1.0)
    today = bars[-1].timestamp.date() + timedelta(days=1)
    mock_client = MagicMock()
    mock_client.get_stock_bars.return_value = {"SPY": bars}

    mult, n, is_event, detail = calib.calibrate_em_multiplier(
        mock_client, "SPY", today, lookback_days=30, vol_window=20, min_samples=20, feed=DataFeed.IEX,
    )
    assert mult is None, f"expected None with only {len(bars)} bars, got {mult}"
    print(f"PASS: insufficient data correctly returns None (detail={detail!r})")


if __name__ == "__main__":
    test_calibration_recovers_known_percentile()
    test_event_days_calibrate_wider_than_ordinary()
    test_insufficient_data_falls_back_to_none()
    print("\nAll em_multiplier_calibration tests passed.")
